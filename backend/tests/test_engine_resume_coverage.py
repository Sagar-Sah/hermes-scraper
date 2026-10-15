"""Resume coverage distinguishes fresh observations, retained rows and skips."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from crawler.config import Settings
from crawler.engine import Engine, row_identity
from crawler.service_context import canonical_service_context
from crawler.store import Event, Product, Run, Store
from test_service_context import service_observation


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    yield result
    result.engine.dispose()


def row(proof, *, provenance="direct_quote", amount="5.60"):
    services = service_observation(proof, delivery=proof == "31")
    result = {
        "record_schema_version": 2,
        "product_url": "https://www.printograph.com/products/business-cards/order",
        "title": "Cards",
        "source_product_id": "1",
        "configuration": {"size": {"value": "101", "label": "Standard"}},
        "quantity": 50,
        "currency": "USD",
        "subtotal": amount,
        "unit_price": "0.11200000" if amount == "5.60" else "0.17200000",
        "production": {"value": "1"},
        "provenance": provenance,
        "verification_status": (
            "pending" if provenance == "source_table" else "not_selected"
        ),
        "evidence_ids": ["saved-" + proof],
        "pricing_scope": "displayed_price",
        "pricing_context": {
            "schema_version": 2,
            "store_type": "broker",
            "source_product_type": "1",
            "service_context": canonical_service_context(services),
        },
        **services,
    }
    result["id"] = row_identity(result)
    return result


async def run_resume(store, monkeypatch, *, outcome, source_status="partial"):
    saved = row("31", provenance="source_table")
    with store.session() as session:
        run = Run(name="Scoped resume", spec={"adapter": "browser"})
        session.add(run)
        session.flush()
        product = Product(run_id=run.id, url=saved["product_url"])
        session.add(product)
        session.flush()
        run_id, product_id = run.id, product.id

    class BrowserFixture:
        async def extract(self, spec, transport, emit):
            if outcome == "reobserved":
                yield deepcopy(saved)
            else:
                if outcome == "skipped":
                    transport.browser_progress.skipped_ids.add(saved["id"])
                yield row("100", amount="8.60")
            await emit(
                "coverage",
                {
                    "status": source_status,
                    "reasons": ["fixture_partial"],
                    "count_is_exact": True,
                    "source_revalidated_on_resume": True,
                },
            )

    monkeypatch.setattr("crawler.adapters.browser.BrowserAdapter", BrowserFixture)
    engine = Engine(store)
    try:
        job = store.claim(engine.owner)
        store.append_rows(product_id, job["epoch"], [deepcopy(saved)])
        await engine.process(job)
    finally:
        await engine.client.aclose()
        await engine.browser_pool.close()
    with store.session() as session:
        coverage = list(
            session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "coverage")
            )
        )[-1].payload
        product = session.get(Product, product_id)
        state = (product.status, product.coverage_status)
        errors = list(
            session.scalars(
                select(Event).where(
                    Event.run_id == run_id, Event.type == "product_error"
                )
            )
        )
        error_codes = [event.payload["code"] for event in errors]
    return (
        saved,
        list(store.iter_rows(product_id=product_id)),
        coverage,
        state,
        error_codes,
    )


@pytest.mark.asyncio
async def test_changed_service_partial_resume_retains_both_scopes_without_claiming_revalidation(
    store, monkeypatch
):
    saved, rows, coverage, state, errors = await run_resume(
        store, monkeypatch, outcome="different_service"
    )
    assert len(rows) == 2 and len({item["id"] for item in rows}) == 2
    assert next(item for item in rows if item["id"] == saved["id"]) == saved
    assert {item["subtotal"] for item in rows} == {"5.60", "8.60"}
    assert coverage["retained_prior_rows_without_revalidation"] == 1
    assert coverage["unvisited_prior_rows"] == 1
    assert coverage["source_revalidated_on_resume"] is False
    assert coverage["count_is_exact"] is False
    assert coverage["reasons"] == ["fixture_partial", "unvisited_prior_rows"]
    assert state == ("partial", "partial") and not errors


@pytest.mark.asyncio
async def test_journal_skip_is_retained_without_revalidation_but_not_unvisited(
    store, monkeypatch
):
    _, rows, coverage, state, errors = await run_resume(
        store, monkeypatch, outcome="skipped"
    )
    assert len(rows) == 2
    assert coverage["retained_prior_rows_without_revalidation"] == 1
    assert coverage["unvisited_prior_rows"] == 0
    assert coverage["source_revalidated_on_resume"] is False
    assert "unvisited_prior_rows" not in coverage["reasons"]
    assert state == ("partial", "partial") and not errors


@pytest.mark.asyncio
async def test_matching_observation_is_revalidated_and_deduplicated(store, monkeypatch):
    _, rows, coverage, _, errors = await run_resume(
        store, monkeypatch, outcome="reobserved"
    )
    assert len(rows) == 1
    assert coverage["retained_prior_rows_without_revalidation"] == 0
    assert coverage["unvisited_prior_rows"] == 0
    assert coverage["source_revalidated_on_resume"] is True
    assert not errors


@pytest.mark.parametrize("status", ["complete", "visible_domain_exhausted"])
@pytest.mark.asyncio
async def test_full_domain_disappearance_still_stops_after_honest_coverage_event(
    store, monkeypatch, status
):
    _, rows, coverage, state, errors = await run_resume(
        store, monkeypatch, outcome="different_service", source_status=status
    )
    assert len(rows) == 2
    assert coverage["status"] == status
    assert coverage["unvisited_prior_rows"] == 1
    assert coverage["source_revalidated_on_resume"] is False
    assert state[0] == "needs_attention"
    assert errors == ["snapshot_changed"]


@pytest.mark.asyncio
async def test_diagnosed_run_rejects_wrong_fixed_service_before_saving(store, monkeypatch):
    from crawler.diagnosis_run import compile_browser_policy
    from test_product_finalization import diagnosis
    d = diagnosis()
    spec = compile_browser_policy(d, [d['url']], 'browser', {})
    with store.session() as session:
        run = Run(name='Fixed-service guard', spec=spec)
        session.add(run)
        session.flush()
        product = Product(run_id=run.id, url=d['url'])
        session.add(product)
        session.flush()
        run_id, product_id = run.id, product.id
    class WrongService:
        async def extract(self, spec, transport, emit):
            assert spec['selection_filters']['proofType'] == '31'
            yield row('100', amount='8.60')
    monkeypatch.setattr('crawler.adapters.browser.BrowserAdapter', WrongService)
    engine = Engine(store)
    try:
        job = store.claim(engine.owner)
        await engine.process(job)
    finally:
        await engine.client.aclose()
        await engine.browser_pool.close()
    assert list(store.iter_rows(product_id=product_id)) == []
    with store.session() as session:
        errors = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.type == 'product_error')))
        assert errors[-1].payload['code'] == 'diagnosis_selection_mismatch'
