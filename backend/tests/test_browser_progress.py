"""Crash safety and real local-browser completed-branch resume."""

import asyncio
from collections import Counter
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from crawler.adapters.printograph import AdapterError
from crawler.browser_progress import BrowserProgress, CoverageNotes, semantic_code_hash
from crawler.config import Settings
from crawler.engine import row_identity
from crawler.store import Batch, BrowserBranch, Product, Run, Store
from crawler.transport import ExtractionError, RunInterrupted
from crawler.service_context import canonical_service_context
from test_service_context import service_observation
from test_browser_traversal import local_page, coverage


@pytest.fixture
def durable_store(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with store.session() as session:
        run = Run(name="Journal fixture", status="running")
        session.add(run)
        session.flush()
        product = Product(
            run_id=run.id,
            url="https://fixture.invalid/product",
            status="running",
            epoch=1,
        )
        session.add(product)
        session.flush()
        ids = run.id, product.id
    yield store, *ids
    store.engine.dispose()


async def journal_for(durable_store, scope="test-v1"):
    store, rid, pid = durable_store
    known = {row["id"] for row in store.iter_rows(product_id=pid)}
    return await BrowserProgress(store, rid, pid, 1, scope, known).load()


def commit_rows(durable_store, journal):
    store, _, pid = durable_store
    known = {row["id"] for row in store.iter_rows(product_id=pid)}

    async def commit(row):
        row["id"] = row_identity(row)
        if row["id"] not in known:
            await asyncio.to_thread(store.append_rows, pid, 1, [row])
            known.add(row["id"])
        journal.acknowledge(row["id"], row)

    return commit


def browser_page_html():
    return """<!doctype html><h1>Durable branch fixture</h1><div id="order-form">
<label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option><option value="250">250</option></select></div>
<div id="subtotal">$5.00</div><div id="ajaxLoader" style="display:none">Loading</div><script>
const paper=document.querySelector('#paper'),quantity=document.querySelector('#quantity');
async function quote(){document.querySelector('#ajaxLoader').style.display='block';const response=await fetch('/service/rest/v1/products?paper='+paper.value+'&quantity='+quantity.value);if(response.ok){const result=await response.json();document.querySelector('#subtotal').textContent='$'+result.subtotal;}document.querySelector('#ajaxLoader').style.display='none';}
paper.addEventListener('change',()=>{quantity.value='100';quote();});quantity.addEventListener('change',quote);
</script>"""


@pytest.mark.asyncio
async def test_crash_resume_skips_durable_complete_branch_and_keeps_unvisited_sibling(
    local_page, durable_store
):
    requests = Counter()
    fail_b = True

    def response(path):
        query = parse_qs(urlparse(path).query)
        paper, quantity = query["paper"][0], int(query["quantity"][0])
        requests[paper] += 1
        if paper == "b" and fail_b:
            return 503, {"error": "local fixture disconnect"}
        return 200, {"subtotal": f"{quantity / 20:.2f}"}

    first = await journal_for(durable_store)
    with pytest.raises(AdapterError, match="HTTP 503"):
        await local_page(
            browser_page_html(),
            response=response,
            journal=first,
            on_row=commit_rows(durable_store, first),
        )
    store, _, pid = durable_store
    assert len(list(store.iter_rows(product_id=pid))) == 2
    with store.session() as session:
        assert [item.prefix for item in session.scalars(select(BrowserBranch))] == [
            {"paper": "a"}
        ]
    first_a_requests = requests["a"]

    # A new journal and browser context stand in for an application restart.
    second = await journal_for(durable_store)
    fail_b = False
    emitted, events = await local_page(
        browser_page_html(),
        response=response,
        journal=second,
        on_row=commit_rows(durable_store, second),
    )
    assert requests["a"] == first_a_requests  # No lookup/requote for finished A.
    assert [row["configuration"]["paper"]["value"] for row in emitted] == ["b", "b"]
    saved = list(store.iter_rows(product_id=pid))
    assert {
        (row["configuration"]["paper"]["value"], row["quantity"]) for row in saved
    } == {
        ("a", 100),
        ("a", 250),
        ("b", 100),
        ("b", 250),
    }
    assert len(saved) == 4
    assert second.skipped_ids == {
        row["id"] for row in saved if row["configuration"]["paper"]["value"] == "a"
    }
    assert coverage(events)["source_revalidated_on_resume"] is False
    assert coverage(events)["status"] == "partial"
    assert not coverage(events)["count_is_exact"]
    assert coverage(events)["resumed_completed_branches"] == 1
    with store.session() as session:
        markers = list(session.scalars(select(BrowserBranch)))
        assert {tuple(item.prefix.items()) for item in markers} == {
            (("paper", "a"),),
            (("paper", "b"),),
        }
        assert sum(item.row_count for item in markers) == 4


@pytest.mark.asyncio
async def test_unacknowledged_yield_cannot_create_completed_marker(
    local_page, durable_store
):
    journal = await journal_for(durable_store)

    async def fail_before_commit(row):
        raise RuntimeError("local crash before durable commit")

    with pytest.raises(RuntimeError, match="before durable"):
        await local_page(
            browser_page_html(), journal=journal, on_row=fail_before_commit
        )
    store, _, pid = durable_store
    assert not list(store.iter_rows(product_id=pid))
    with store.session() as session:
        assert not list(session.scalars(select(BrowserBranch)))


@pytest.mark.asyncio
async def test_partial_branch_is_not_journaled_but_healthy_sibling_is(
    local_page, durable_store
):
    html = """<!doctype html><h1>Partial branch</h1><div id="order-form">
<label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select>
<div id="unsupported"><label for="width">Width</label><input id="width" type="number" value="1"></div></div><div id="subtotal">$5.00</div>
<script>document.querySelector('#paper').addEventListener('change',()=>document.querySelector('#unsupported').style.display=document.querySelector('#paper').value==='a'?'block':'none');</script>"""
    journal = await journal_for(durable_store)
    _, events = await local_page(
        html, journal=journal, on_row=commit_rows(durable_store, journal)
    )
    assert coverage(events)["status"] == "partial"
    store, _, _ = durable_store
    with store.session() as session:
        prefixes = [item.prefix for item in session.scalars(select(BrowserBranch))]
    assert prefixes == [{"paper": "b"}]


@pytest.mark.asyncio
async def test_checkpoint_scope_and_durable_identity_validation(durable_store):
    store, rid, pid = durable_store
    journal = await journal_for(durable_store)
    frame = journal.begin({"paper": "a"})
    row = {"id": "one", "configuration": {"paper": {"value": "a"}}, "quantity": 100}
    store.append_rows(pid, 1, [row])
    journal.acknowledge("one", row)
    await journal.complete(frame, set())
    journal.end(frame)
    assert not (await journal_for(durable_store, "changed-spec-or-version")).contains(
        {"paper": "a"}
    )
    with store.session() as session:
        for batch in session.scalars(select(Batch)):
            session.delete(batch)
    with pytest.raises(ExtractionError, match="missing durable rows"):
        await journal_for(durable_store)


def test_semantic_hash_ignores_formatting_and_comments_but_not_behavior():
    assert semantic_code_hash("x=1\n") == semantic_code_hash("# comment\nx = 1\n\n")
    assert semantic_code_hash("x=1\n") != semantic_code_hash("x=2\n")


def test_repeated_gap_and_verification_only_note_have_different_meanings():
    notes = CoverageNotes()
    notes.add("quantity_candidates_pending_verification")
    assert notes.generation == 0
    notes.update(["choice_disappeared"])
    first = notes.generation
    notes.update(["choice_disappeared"])
    assert notes.generation == first + 1


@pytest.mark.asyncio
async def test_static_outside_domain_gaps_survive_completed_branch_resume(
    local_page, durable_store
):
    html = browser_page_html().replace(
        'id="order-form"', 'id="order-form" data-printproducttype="1"'
    )
    html = html.replace(
        '<label for="paper">',
        '<label for="size">Size</label><select id="size"><option value="101">Standard</option><option value="custom">Custom Size</option></select><label for="paper">',
    )
    html += """<div id="collapseOne" style="display:none"><input type="radio" name="proofType" id="proof" value="31" checked><label for="proof">Instant Processing</label><input type="radio" name="fileDeliveryForIop" id="upload" value="5" checked><label for="upload">Upload Now</label></div>"""
    requests, fail_b = Counter(), True

    def response(path):
        query = parse_qs(urlparse(path).query)
        paper, quantity = query["paper"][0], int(query["quantity"][0])
        requests[paper] += 1
        return (
            (503, {"error": "fixture crash"})
            if paper == "b" and fail_b
            else (200, {"subtotal": f"{quantity / 20:.2f}"})
        )

    first = await journal_for(durable_store)
    with pytest.raises(AdapterError, match="HTTP 503"):
        await local_page(
            html,
            response=response,
            journal=first,
            on_row=commit_rows(durable_store, first),
        )
    assert first.contains(
        {"size": "101", "paper": "a"}, canonical_service_context(service_observation())
    )
    first_requests = requests["a"]
    second = await journal_for(durable_store)
    fail_b = False
    _, events = await local_page(
        html,
        response=response,
        journal=second,
        on_row=commit_rows(durable_store, second),
    )
    # The child is already selected on load. Its current service context is
    # observed before skipping without a redundant price request.
    assert requests["a"] == first_requests
    report = coverage(events)
    assert report["status"] == "partial" and not report["count_is_exact"]
    assert set(report["reasons"]) == {
        "custom_values:size",
        "collapsed_service_control:proofType",
        "collapsed_service_control:fileDeliveryForIop",
        "completed_branches_not_revalidated",
    }
    assert report["resumed_completed_branches"] == 1
    assert report["source_revalidated_on_resume"] is False


def test_selected_or_unknown_custom_control_is_still_a_blocking_gap():
    notes = CoverageNotes()
    notes.observe_snapshot(
        {
            "product_type": "36",
            "unsupported": ["custom_values:size"],
            "controls": [
                {
                    "key": "size",
                    "kind": "select",
                    "value": "custom",
                    "choices": [{"value": "101"}],
                }
            ],
        }
    )
    assert notes.generation == 1
    notes.observe_snapshot(
        {"product_type": "36", "unsupported": ["custom_values:width"], "controls": []}
    )
    assert notes.generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor", [{}, {"size": "101"}])
async def test_disappeared_completed_descendant_cannot_be_compacted(
    durable_store, ancestor
):
    store, _, pid = durable_store
    journal = await journal_for(durable_store)
    completed = journal.begin({"size": "101", "paper": "a"})
    row = {"id": "one", "configuration": {"size": "101", "paper": "a"}, "quantity": 100}
    store.append_rows(pid, 1, [row])
    journal.acknowledge("one", row)
    await journal.complete(completed, {"collapsed_service_control:proofType"})
    journal.end(completed)
    parent = journal.begin(ancestor)
    # The freshly observed domain never traversed/skipped the old child A.
    with pytest.raises(ExtractionError) as failure:
        await journal.complete(parent, {"collapsed_service_control:proofType"})
    journal.end(parent)
    assert failure.value.code == "snapshot_changed"
    with store.session() as session:
        assert [item.prefix for item in session.scalars(select(BrowserBranch))] == [
            {"size": "101", "paper": "a"}
        ]


@pytest.mark.asyncio
async def test_superseded_worker_cannot_persist_branch_completion(durable_store):
    store, _, pid = durable_store
    journal = await journal_for(durable_store)
    frame = journal.begin({"paper": "a"})
    row = {"id": "one", "configuration": {"paper": "a"}, "quantity": 100}
    store.append_rows(pid, 1, [row])
    journal.acknowledge("one", row)
    with store.session() as session:
        session.get(Product, pid).epoch = 2
    with pytest.raises(RunInterrupted, match="superseded"):
        await journal.complete(frame, set())
    journal.end(frame)
    with store.session() as session:
        assert not list(session.scalars(select(BrowserBranch)))


@pytest.mark.asyncio
async def test_service_scoped_checkpoints_coexist_and_never_cross_skip(durable_store):
    store, _, pid = durable_store
    journal = await journal_for(durable_store)
    prefix = {"paper": "a"}
    instant = canonical_service_context(service_observation())
    manual = canonical_service_context(service_observation("100"))
    for ident, context in [("instant", instant), ("manual", manual)]:
        frame = journal.begin(prefix, context)
        row = {
            "id": ident,
            "configuration": {"paper": "a"},
            "quantity": 100,
            "pricing_context": {"service_context": context},
        }
        store.append_rows(pid, 1, [row])
        journal.acknowledge(ident, row)
        await journal.complete(frame, set())
        journal.end(frame)
    resumed = await journal_for(durable_store)
    assert resumed.skip(prefix, instant)["row_ids"] == {"instant"}
    assert resumed.skip(prefix, manual)["row_ids"] == {"manual"}
    assert resumed.skip(prefix) is None
    assert resumed.skip(prefix, canonical_service_context({})) is None
    with store.session() as session:
        assert len(list(session.scalars(select(BrowserBranch)))) == 2


@pytest.mark.asyncio
async def test_changed_or_unknown_services_cannot_complete_parent(durable_store):
    store, _, pid = durable_store
    journal = await journal_for(durable_store)
    instant = canonical_service_context(service_observation())
    manual = canonical_service_context(service_observation("100"))
    parent = journal.begin({"size": "101"}, instant)
    child = journal.begin({"size": "101", "paper": "a"}, manual)
    row = {
        "id": "manual",
        "configuration": {"size": "101", "paper": "a"},
        "quantity": 100,
        "pricing_context": {"service_context": manual},
    }
    store.append_rows(pid, 1, [row])
    journal.acknowledge("manual", row)
    await journal.complete(child, set())
    journal.end(child)
    await journal.complete(parent, set())
    journal.end(parent)
    unknown = journal.begin({"size": "101"}, canonical_service_context({}))
    journal.acknowledge("manual", row)
    await journal.complete(unknown, set())
    journal.end(unknown)
    resumed = await journal_for(durable_store)
    assert resumed.contains(child.prefix, manual)
    assert not resumed.contains(parent.prefix, instant)
    assert len(resumed.completed) == 1
