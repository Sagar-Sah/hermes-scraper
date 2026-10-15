"""Durable transient recovery and immutable legacy service-scope separation."""

from copy import deepcopy
import json
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
from sqlalchemy import select

import crawler.engine as engine_module
from crawler.config import Settings
from crawler.engine import Engine, row_identity
from crawler.exporter import export_rows, query_price_analytics
from crawler.catalog_export import combine_catalog
from crawler.pacing import retry_after_seconds
from crawler.record_scope import is_legacy_unscoped
from crawler.service_context import canonical_service_context
from crawler.store import Event, Product, Run, Store
from crawler.transport import ExtractionError
from test_engine import create_job, observed_row, store
from test_service_context import service_observation


def test_retry_delay_and_budget_survive_store_restart_and_fence_old_worker(
    store, monkeypatch
):
    rid, pid = create_job(store)
    clock = [1000.0]
    monkeypatch.setattr(store, "now", lambda _: clock[0])
    job = store.claim("first")
    original_epoch = job["epoch"]
    for attempt in range(1, 4):
        retry = store.retry_product(
            rid, pid, job["epoch"], "browser_network_error", "reset"
        )
        assert retry["attempt"] == attempt
        assert retry["delay_seconds"] == 5 * 2 ** (attempt - 1)
        assert store.claim("too early") is None
        assert not store.heartbeat(pid, job["epoch"])
        assert (
            store.retry_product(
                rid, pid, job["epoch"], "browser_network_error", "stale"
            )
            is None
        )
        reopened = Store(Settings(data_dir=store.root, s3_bucket=""))
        try:
            monkeypatch.setattr(reopened, "now", lambda _: clock[0])
            assert reopened.claim("early restart") is None
            clock[0] = retry["retry_at"]
            job = reopened.claim("restarted")
            assert job and job["epoch"] > original_epoch
            with reopened.session() as session:
                assert session.get(Product, pid).error is None
        finally:
            reopened.engine.dispose()
    assert (
        store.retry_product(rid, pid, job["epoch"], "browser_network_error", "reset")
        is None
    )
    store.append_rows(pid, job["epoch"], [{"id": "new durable progress"}])
    retry = store.retry_product(rid, pid, job["epoch"], "source_unavailable", "HTTP503")
    assert retry["attempt"] == 1 and retry["row_count"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "access_required",
        "snapshot_changed",
        "invalid_price",
        "source_error",
        "unscoped_quote",
    ],
)
def test_auth_integrity_and_nontransient_source_errors_are_not_retried(store, code):
    rid, pid = create_job(store)
    job = store.claim("worker")
    assert store.retry_product(rid, pid, job["epoch"], code, "failure") is None
    with store.session() as session:
        assert not list(
            session.scalars(
                select(Event).where(Event.type == "product_retry_scheduled")
            )
        )


def test_paused_run_cannot_schedule_recovery(store):
    rid, pid = create_job(store)
    job = store.claim("worker")
    with store.session() as session:
        session.get(Run, rid).status = "paused"
    assert store.retry_product(rid, pid, job["epoch"], "network_error", "reset") is None


@pytest.mark.asyncio
async def test_engine_transient_recovery_deduplicates_durable_rows(store, monkeypatch):
    rid, pid = create_job(store)
    attempts = 0
    clock = [1000.0]
    monkeypatch.setattr(store, "now", lambda _: clock[0])

    class Recovering:
        async def extract(self, spec, transport, emit):
            nonlocal attempts
            attempts += 1
            for index in range(501):
                if attempts == 1 and index == 500:
                    raise ExtractionError("connection reset", "browser_network_error")
                yield observed_row(spec, index)
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", Recovering)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        with store.session() as session:
            product = session.get(Product, pid)
            assert product.status == "queued" and product.row_count == 500
            assert session.get(Run, rid).status == "running"
            clock[0] = product.lease_until
        await engine.process(store.claim(engine.owner))
        await engine.finalize(rid)
        with store.session() as session:
            assert session.get(Product, pid).status == "completed"
            assert session.get(Run, rid).status == "completed"
            assert not list(
                session.scalars(select(Event).where(Event.type == "product_error"))
            )
        rows = list(store.iter_rows(product_id=pid))
        assert len(rows) == len({row["id"] for row in rows}) == 501
    finally:
        await engine.client.aclose()


@pytest.mark.parametrize(
    "header,expected",
    [(None, 30), ("7", 7), ("-2", 0), ("nan", 30), ("inf", 30), ("bad", 30)],
)
def test_retry_after_delta_and_invalid_values(header, expected):
    assert retry_after_seconds(header, now=1000) == expected


def test_retry_after_http_dates():
    future = format_datetime(datetime.fromtimestamp(1007, tz=timezone.utc), usegmt=True)
    assert retry_after_seconds(future, now=1000) == 7
    assert retry_after_seconds(future, now=1008) == 0


def scoped_row(service="31"):
    return {
        **observed_row(
            {"url": "https://www.printograph.com/products/business-cards/order"}
        ),
        "source_product_id": "1",
        "provenance": "direct_quote",
        "record_schema_version": 2,
        **service_observation(service),
        "pricing_context": {
            "schema_version": 2,
            "service_context": canonical_service_context(service_observation(service)),
        },
    }


@pytest.mark.asyncio
async def test_legacy_quote_is_retained_but_cannot_collide_with_new_service_scoped_quote(
    store, monkeypatch, tmp_path
):
    rid, pid = create_job(store)
    job = store.claim("seed")
    legacy = scoped_row()
    for key in (
        "record_schema_version",
        "pricing_context",
        "service_context",
        "service_context_observed",
    ):
        legacy.pop(key)
    legacy["id"] = row_identity(legacy)
    original = deepcopy(legacy)
    store.append_rows(pid, job["epoch"], [legacy])

    class Scoped:
        async def extract(self, spec, transport, emit):
            yield {**scoped_row("100"), "subtotal": "13.00", "unit_price": "13.00"}
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", Scoped)
    engine = Engine(store)
    try:
        await engine.process(job)
        await engine.finalize(rid)
        assert list(store.iter_rows(product_id=pid))[0] == original
        current = list(store.current_rows(product_id=pid))
        assert len(current) == 1 and not is_legacy_unscoped(current[0])
        counts = store.record_counts(pid)
        assert counts == {
            "source_row_count": 2,
            "legacy_row_count": 1,
            "scoped_row_count": 1,
            "record_scope": "service_context_v2",
        }
        with store.session() as session:
            manifest = session.get(Product, pid).export_manifest
            assert manifest["row_count"] == 1 and manifest["legacy_row_count"] == 1
            assert manifest["source_row_count"] == 2
        combined = combine_catalog([manifest], tmp_path / "combined")
        assert combined["row_count"] == 1 and combined["source_row_count"] == 2
        assert (
            combined["legacy_row_count"] == 1
            and combined["record_scope"] == "service_context_v2"
        )
        assert combined["created_at"] and combined["generation"]
        assert (
            json.loads(
                (
                    tmp_path / "combined" / combined["generation"] / "manifest.json"
                ).read_text()
            )
            == combined
        )
    finally:
        await engine.client.aclose()


def test_export_analytics_separate_services_and_production(tmp_path):
    first = scoped_row()
    second = scoped_row("100")
    third = {**first, "production": {"mode": "rush"}}
    manifest = export_rows([first, second, third], tmp_path)
    parquet = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.parquet"
    )
    summaries = query_price_analytics(parquet)
    assert len(summaries) == 3 and all(item["row_count"] == 1 for item in summaries)


@pytest.mark.asyncio
async def test_equal_count_legacy_manifest_is_rebuilt_after_scope_classification(store):
    rid, pid = create_job(store)
    job = store.claim("seed")
    legacy = scoped_row()
    legacy.pop("record_schema_version")
    legacy["id"] = row_identity(legacy)
    store.append_rows(pid, job["epoch"], [legacy])
    old = export_rows([legacy], store.root / "old-export")
    store.set_record_scope(pid, job["epoch"], 1)
    with store.session() as session:
        product = session.get(Product, pid)
        product.status, product.export_manifest = "needs_attention", old
        session.get(Run, rid).status = "exporting"
    engine = Engine(store)
    try:
        await engine.finalize(rid)
        with store.session() as session:
            fresh = session.get(Product, pid).export_manifest
        assert fresh["generation"] != old["generation"]
        assert fresh["row_count"] == 0 and fresh["source_row_count"] == 1
        assert (
            fresh["legacy_row_count"] == 1
            and fresh["record_scope"] == "service_context_v2"
        )
        assert all(Path(item["path"]).exists() for item in old["items"])
    finally:
        await engine.client.aclose()
