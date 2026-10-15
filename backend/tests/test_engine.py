import asyncio

import pytest
from sqlalchemy import select

import crawler.engine as engine_module
from crawler.config import Settings
from crawler.engine import Engine
from crawler.store import Event, Product, Run, Store
from crawler.transport import ExtractionError


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    yield result
    result.engine.dispose()


def create_job(store, *, count=3):
    with store.session() as session:
        run = Run(
            name="Engine fixture",
            spec={"adapter": "fixture", "fixture_rows_per_product": count},
        )
        session.add(run)
        session.flush()
        product = Product(run_id=run.id, url="fixture://catalog/test")
        session.add(product)
        session.flush()
        ids = run.id, product.id
    return ids


def state(store, run_id, product_id):
    with store.session() as session:
        run, product = session.get(Run, run_id), session.get(Product, product_id)
        return run.status, product.status, product.row_count, product.export_manifest


def observed_row(spec, index=0, price="10.00"):
    return {
        "product_url": spec["url"],
        "title": "Fixture",
        "source_product_id": "one",
        "configuration": {"variant": str(index)},
        "quantity": 1,
        "currency": "USD",
        "subtotal": price,
        "unit_price": price,
        "provenance": "synthetic_fixture",
        "verification_status": "fixture_verified",
        "evidence_ids": [],
        "production": {"mode": "regular"},
    }


@pytest.mark.asyncio
async def test_fixture_completion_exports_exact_unique_rows(store):
    run_id, product_id = create_job(store, count=12)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        run_status, product_status, count, manifest = state(store, run_id, product_id)
        assert run_status == "completed" and product_status == "completed"
        assert count == 12 == manifest["row_count"]
        rows = list(store.iter_rows(product_id=product_id))
        assert len({row["id"] for row in rows}) == 12
        assert all(row["fixture"] for row in rows)
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_checkpoint_resume_deduplicates_committed_rows(store, monkeypatch):
    run_id, product_id = create_job(store)
    attempts = 0

    class Restartable:
        async def extract(self, spec, transport, emit):
            nonlocal attempts
            attempts += 1
            for index in range(600):
                if attempts == 1 and index == 505:
                    raise ExtractionError("fixture disconnect")
                yield observed_row(spec, index)
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", Restartable)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        assert state(store, run_id, product_id)[:3] == (
            "needs_attention",
            "needs_attention",
            500,
        )
        with store.session() as session:
            session.get(Run, run_id).status = "queued"
            session.get(Product, product_id).status = "queued"
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        rows = list(store.iter_rows(product_id=product_id))
        assert len(rows) == len({row["id"] for row in rows}) == 600
        assert state(store, run_id, product_id)[:3] == ("completed", "completed", 600)
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_pause_checkpoint_is_not_misreported_as_completed(store, monkeypatch):
    run_id, product_id = create_job(store)

    class Pausing:
        async def extract(self, spec, transport, emit):
            yield observed_row(spec)
            with store.session() as session:
                session.get(Run, run_id).status = "paused"
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", Pausing)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        assert state(store, run_id, product_id)[:2] == ("paused", "queued")
        assert store.claim("other") is None
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_changed_price_on_resume_requires_a_new_snapshot_visibly(
    store, monkeypatch
):
    run_id, product_id = create_job(store)
    attempts = 0

    class ChangingSource:
        async def extract(self, spec, transport, emit):
            nonlocal attempts
            attempts += 1
            yield observed_row(spec, price="10.00" if attempts == 1 else "20.00")
            await emit(
                "coverage", {"status": "partial" if attempts == 1 else "complete"}
            )

    monkeypatch.setattr(engine_module, "FixtureAdapter", ChangingSource)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        with store.session() as session:
            run = session.get(Run, run_id)
            run.status = "queued"
            run.spec = {**run.spec, "refresh": True}
            session.get(Product, product_id).status = "queued"
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        current_rows = list(store.iter_rows(product_id=product_id))
        assert current_rows[-1]["subtotal"] == "10.00"  # immutable historical snapshot
        assert state(store, run_id, product_id)[:2] == (
            "needs_attention",
            "needs_attention",
        )
        with store.session() as session:
            errors = list(
                session.scalars(
                    select(Event).where(
                        Event.run_id == run_id, Event.type == "product_error"
                    )
                )
            )
        assert errors[-1].payload["code"] == "snapshot_changed"
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_cancel_during_export_cannot_commit_product_completion(
    store, monkeypatch
):
    from crawler import exporter

    run_id, product_id = create_job(store)

    def cancelled_export(rows, output_dir, **kwargs):
        count = sum(1 for _ in rows)
        with store.session() as session:
            session.get(Run, run_id).status = "cancelled"
        return {"items": [], "row_count": count}

    monkeypatch.setattr(exporter, "export_rows", cancelled_export)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        run_status, product_status, _, manifest = state(store, run_id, product_id)
        assert run_status == "cancelled"
        assert product_status == "cancelled"
        assert not manifest
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_catalog_export_failure_cannot_leave_run_fully_completed(
    store, monkeypatch
):
    from crawler import catalog_export

    run_id, product_id = create_job(store)

    def failed_catalog(*args, **kwargs):
        raise OSError("fixture disk full")

    monkeypatch.setattr(catalog_export, "combine_catalog", failed_catalog)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        assert state(store, run_id, product_id)[0] != "completed"
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_export_failure_resume_retries_catalog_without_reextracting(
    store, monkeypatch
):
    import crawler.app as api
    from crawler import catalog_export

    run_id, product_id = create_job(store, count=3)
    original = catalog_export.combine_catalog

    def failed_catalog(*args, **kwargs):
        raise OSError("fixture disk full")

    monkeypatch.setattr(catalog_export, "combine_catalog", failed_catalog)
    monkeypatch.setattr(api, "store", store)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        assert state(store, run_id, product_id)[:3] == (
            "needs_attention",
            "completed",
            3,
        )
        monkeypatch.setattr(catalog_export, "combine_catalog", original)
        api.control(run_id, "resume")
        assert store.status(run_id) == "exporting"
        assert store.claim("unexpected-worker") is None
        await engine.finalize(run_id)
        assert state(store, run_id, product_id)[:3] == ("completed", "completed", 3)
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_finish_run_waits_for_every_active_product_and_preserves_gaps(store):
    run_id, first = create_job(store)
    with store.session() as session:
        session.get(Product, first).status = "completed"
        other = Product(run_id=run_id, url="fixture://other", status="running")
        session.add(other)
        session.flush()
        second = other.id
    engine = Engine(store)
    try:
        assert not engine.finish_run(run_id)
        assert store.status(run_id) == "queued"
        with store.session() as session:
            session.get(Product, second).status = "partial"
        assert engine.finish_run(run_id)
        await engine.finalize(run_id)
        assert store.status(run_id) == "completed_with_gaps"
        assert not engine.finish_run(run_id)
    finally:
        await engine.client.aclose()


@pytest.mark.parametrize("committed_first", [False, True])
@pytest.mark.asyncio
async def test_same_active_pass_price_conflict_fails_before_and_after_checkpoint(
    store, monkeypatch, committed_first
):
    run_id, product_id = create_job(store)

    class ConflictingSource:
        async def extract(self, spec, transport, emit):
            for index in range(500 if committed_first else 1):
                yield observed_row(spec, index, price="10.00")
            yield observed_row(spec, 0, price="20.00")
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", ConflictingSource)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        assert state(store, run_id, product_id)[:2] == (
            "needs_attention",
            "needs_attention",
        )
        rows = list(store.iter_rows(product_id=product_id))
        assert len(rows) == (500 if committed_first else 0)
        assert all(row["subtotal"] == "10.00" for row in rows)
        with store.session() as session:
            errors = list(
                session.scalars(
                    select(Event).where(
                        Event.run_id == run_id, Event.type == "product_error"
                    )
                )
            )
        assert errors[-1].payload["code"] == "snapshot_changed"
    finally:
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_exact_active_pass_repeat_deduplicates_without_claiming_verification_upgrade(
    store, monkeypatch
):
    run_id, product_id = create_job(store)

    class RepeatedSource:
        async def extract(self, spec, transport, emit):
            first = observed_row(spec, price="10.00")
            first["verification_status"] = "pending"
            yield first
            repeated = observed_row(spec, price="10.00")
            repeated["verification_status"] = "verified"
            yield repeated
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", RepeatedSource)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        await engine.finalize(run_id)
        rows = list(store.iter_rows(product_id=product_id))
        assert len(rows) == 1 and rows[0]["subtotal"] == "10.00"
        assert rows[0]["verification_status"] == "pending"
        assert state(store, run_id, product_id)[:3] == ("completed", "completed", 1)
    finally:
        await engine.client.aclose()


def test_identity_ignores_labels_and_observation_state_but_keeps_selected_modes():
    from copy import deepcopy
    from crawler.engine import row_identity

    row = observed_row({"url": "fixture://identity"})
    row["configuration"] = {"corners": {"value": "2", "label": "Corners (+$3)"}}
    row["production"] = {
        "value": "1",
        "label": "Regular 2–4 days",
        "status": "source_displayed",
    }
    candidate = deepcopy(row)
    candidate["configuration"]["corners"]["label"] = "Round corners"
    candidate["production"] = {"value": "1", "status": "requires_configuration_quote"}
    candidate["verification_status"] = "pending"
    candidate["provenance"] = "source_table"
    candidate["pricing_context"] = {"source_base_product_id": "100000021"}
    assert row_identity(row) == row_identity(candidate)
    candidate["production"]["value"] = "3"
    assert row_identity(row) != row_identity(candidate)
    candidate["production"]["value"] = "1"
    candidate["configuration"]["corners"]["value"] = ""
    assert row_identity(row) != row_identity(candidate)


@pytest.mark.asyncio
async def test_resume_reindexes_legacy_presentation_identity_without_duplicating(
    store, monkeypatch
):
    run_id, product_id = create_job(store)
    engine = Engine(store)
    job = store.claim(engine.owner)
    saved = observed_row({"url": job["url"]})
    saved["id"] = "legacy-presentation-dependent-id"
    saved["production"] = {
        "value": "1",
        "label": "Regular 2–4 days",
        "status": "source_displayed",
    }
    store.append_rows(product_id, job["epoch"], [saved])

    class Replayed:
        async def extract(self, spec, transport, emit):
            row = observed_row(spec)
            row["production"] = {"value": "1", "status": "requires_configuration_quote"}
            yield row
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", Replayed)
    try:
        await engine.process(job)
        rows = list(store.iter_rows(product_id=product_id))
        assert len(rows) == 1
        assert rows[0]["id"] == "legacy-presentation-dependent-id"
        assert state(store, run_id, product_id)[1:3] == ("completed", 1)
    finally:
        await engine.client.aclose()


@pytest.mark.parametrize("changed_subtotal", [False, True])
@pytest.mark.asyncio
async def test_resume_compares_source_subtotal_not_legacy_unit_rounding(
    store, monkeypatch, changed_subtotal
):
    """The real source tie is510.65/80000 =0.006383125."""
    from crawler.commerce import unit_price

    run_id, product_id = create_job(store)
    engine = Engine(store)
    job = store.claim(engine.owner)
    saved = observed_row({"url": job["url"]}, price="510.65")
    saved.update(
        id="immutable-direct-quote",
        quantity=80000,
        unit_price="0.00638312",  # historical half-even display
        provenance="direct_quote",
        verification_status="not_selected",
    )
    store.append_rows(product_id, job["epoch"], [saved])

    class ReplayedTable:
        async def extract(self, spec, transport, emit):
            subtotal = "510.66" if changed_subtotal else "510.650"
            row = observed_row(spec, price=subtotal)
            row.update(
                quantity=80000,
                unit_price=unit_price(subtotal, 80000),
                provenance="source_table",
                verification_status="pending",
            )
            yield row
            await emit("coverage", {"status": "complete"})

    monkeypatch.setattr(engine_module, "FixtureAdapter", ReplayedTable)
    try:
        await engine.process(job)
        rows = list(store.iter_rows(product_id=product_id))
        assert len(rows) == 1
        assert rows[0]["id"] == "immutable-direct-quote"
        assert rows[0]["subtotal"] == "510.65"
        assert rows[0]["unit_price"] == "0.00638312"
        assert rows[0]["provenance"] == "direct_quote"
        assert rows[0]["verification_status"] == "not_selected"
        assert state(store, run_id, product_id)[1:3] == (
            "needs_attention" if changed_subtotal else "completed",
            1,
        )
        with store.session() as session:
            errors = list(
                session.scalars(
                    select(Event).where(
                        Event.run_id == run_id, Event.type == "product_error"
                    )
                )
            )
        if changed_subtotal:
            assert errors[-1].payload["code"] == "snapshot_changed"
        else:
            assert not errors
    finally:
        await engine.client.aclose()


def test_price_signature_keeps_exact_subtotal_and_unit_only_price_changes():
    from crawler.engine import price_signature

    first = {"quantity": 80000000, "subtotal": "510.65", "unit_price": "0.00000638"}
    changed = {**first, "subtotal": "510.66"}
    # Both unit prices round to the same eight digits; the source total changed.
    assert price_signature(first)[1] == price_signature(changed)[1]
    assert price_signature(first) != price_signature(changed)
    assert price_signature({"subtotal": None, "unit_price": "1.00"}) != price_signature(
        {"subtotal": None, "unit_price": "1.01"}
    )
