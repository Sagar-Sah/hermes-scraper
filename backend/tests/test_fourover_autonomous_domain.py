"""Prospective domain persistence: intercepted Chromium, no real supplier IO."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_fourover_automatic_profile import load_driver, session_fixture, PRIVATE, SELECTION, SOURCE
from test_fourover_native_proceed_transition import browser_fixture
from test_fourover_native_quantity_expand import install_collapsed, prepare, Q


@pytest.mark.asyncio
async def test_collapsed_model_tiers_are_not_reported_as_expanded_visible_domain(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page)
        await install_collapsed(page)
        observed = await session.capture_native_domain(selection, table)
        assert observed["status"] == "observed"
        assert observed["quantity_controls"]["model_available"] == Q
        assert [int(v["value"]) for v in observed["quantity_controls"]["rendered"]] == Q[:7]
        assert observed["quantity_controls"]["remaining_expand_controls"] == 1
        assert observed["expanded_rendered_domain"] == "unknown"
        assert observed["source_domain_complete"] is False
        assert await page.evaluate("fixture.expandClicks") == 0
        assert await page.evaluate("fixture.requests") == await page.evaluate("privateReads") == 0
        assert len(seen) == 1 and not session.cursor["receipts"]


@pytest.mark.asyncio
async def test_full_native_expansion_persists_every_label_and_bound_child_without_an_extra_click(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page)
        await install_collapsed(page)
        await session.reveal_quantity(selection, table)
        before = await page.evaluate("fixture.expandClicks")
        observed = await session.capture_native_domain(selection, table)
        assert observed["status"] == "observed" and observed["expanded_rendered_domain"] == "verified"
        assert observed["quantity_controls"]["rendered"] == [
            {"value": str(q), "label": str(q), "enabled": True} for q in Q]
        assert observed["child_id"] == "700" and observed["selected_attributes"] == selection["attributes"]
        assert observed["page_generation"] == session.session_id and observed["snapshot_id"] == "a" * 32
        assert observed["price_response_sha256"] == table["sha256"] and observed["document_sha256"] == "d" * 64
        assert observed["scope"] == m.SCOPE and observed["scope_controls"]["sets"]["binding"] == "one_native_job"
        assert observed["production_scope"] == "observed_current_quantity_only"
        assert observed["observed_run_size"] == "250"
        assert observed["production_controls"] == [{"value": "10", "label": "2-4 Business Days", "selected": True, "enabled": True, "visible": True}]
        assert observed["source_domain_complete"] is False  # every quantity's production remains separate
        assert before == await page.evaluate("fixture.expandClicks") == 1
        assert await page.evaluate("fixture.requests") == await page.evaluate("privateReads") == 0
        assert len(seen) == 1 and PRIVATE not in json.dumps(observed)


@pytest.mark.asyncio
async def test_observe_domain_mode_expands_for_low_target_without_selecting_or_quoting(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page)
        selection["run_size"] = 250
        await install_collapsed(page)
        assert len((await session.state())["available_run_sizes"]) == 7
        observed = await session.capture_native_domain(selection, table, expand_all=True)
        assert observed["expanded_rendered_domain"] == "verified"
        assert [int(v["value"]) for v in observed["quantity_controls"]["rendered"]] == Q
        assert (await session.state())["run_size"] == "250"
        assert await page.evaluate("fixture.expandClicks") == 1
        assert await page.evaluate("fixture.requests") == await page.evaluate("privateReads") == 0
        assert len(seen) == 1 and not session.cursor["receipts"] and session.cursor["inflight"] is None
        await session.capture_native_domain(selection, table, expand_all=True)
        assert await page.evaluate("fixture.expandClicks") == 1


@pytest.mark.asyncio
async def test_observe_domain_mode_holds_if_source_expansion_changes_selected_quantity(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page); selection["run_size"] = 250
        await install_collapsed(page)
        await page.evaluate("""()=>{
            const original=fixture.quantityView.collapsibleToggle;
            fixture.quantityView.collapsibleToggle=function(){original.call(this);fixture.quantity(500);fixture.data.runSize(500);fixture.quantityView.lineLimit(99);};
        }""")
        with pytest.raises(m.Held, match="source_quantity_expansion_changed_selection"):
            await session.capture_native_domain(selection, table, expand_all=True)
        assert await page.evaluate("fixture.requests") == 0 and not session.cursor["receipts"]
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_read_only_expansion_does_not_reopen_or_modify_spent_quote_journal(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page); selection["run_size"] = 250
        await install_collapsed(page)
        assert m.reserve_once(tmp_path, SOURCE, selection, "f" * 64)
        journal = next((tmp_path / "fourover-native-observation-selections").glob("*/*.json"))
        before = journal.read_bytes()
        observed = await session.capture_native_domain(selection, table, expand_all=True)
        assert observed["expanded_rendered_domain"] == "verified"
        assert journal.read_bytes() == before and session.prepare_selection(session.catalog["entries"][0], 250) is None
        assert await page.evaluate("fixture.expandClicks") == 1 and await page.evaluate("fixture.requests") == 0
        assert not session.cursor["receipts"] and session.cursor["inflight"] is None and len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "fixture.selected('701')", "fixture.rows.peek()[8].qty=35001",
    "document.querySelector('[name=job_sample]').checked=true",
    "document.querySelector('select').value='11'",
    "fixture.tabs.selectedTab('product.shipping')",
    "const e=document.createElement('input');e.id='run-size-search';e.value='100';document.querySelector('form').append(e)",
])
async def test_unretained_child_scope_or_search_never_certifies_domain(tmp_path, mutation):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, seen):
        selection, table = prepare(m, session, page)
        await install_collapsed(page)
        await page.evaluate(mutation)
        observed = await session.capture_native_domain(selection, table)
        assert observed["status"] == observed["expanded_rendered_domain"] == "unknown"
        assert observed["source_domain_complete"] is False
        assert await page.evaluate("fixture.requests") == 0
        assert PRIVATE not in json.dumps(observed) and len(seen) == 1


@pytest.mark.asyncio
async def test_extra_option_is_recorded_without_reading_its_value_or_private_hidden_fields(tmp_path):
    m = load_driver(); session, _, _ = session_fixture(m, tmp_path)
    async with browser_fixture(m) as (page, _):
        selection, table = prepare(m, session, page)
        await install_collapsed(page)
        await page.evaluate("""privateValue=>{
            const form=document.querySelector('form'),e=document.createElement('select');e.name='extra_finish';
            e.innerHTML='<option value="private">Opaque extra</option>';Object.defineProperty(e,'value',{get(){throw Error('Do not read unknown values');}});form.append(e);
            const hidden=document.createElement('input');hidden.type='hidden';hidden.name='token';hidden.value=privateValue;form.append(hidden);
        }""", PRIVATE)
        observed = await session.capture_native_domain(selection, table)
        assert observed["status"] == "observed"
        assert observed["additional_option_fields"] == [{"tag": "select", "type": "select-one", "name": "extra_finish"}]
        assert PRIVATE not in json.dumps(observed) and "private" not in observed["additional_option_fields"][0].values()
        assert observed["source_domain_complete"] is False


def test_unknown_projection_never_persists_arbitrary_fields_or_reason_text():
    m = load_driver()
    for value in [{"status": "unknown", "reason": PRIVATE}, {"status": "observed", "private": PRIVATE},
                  {"status": "unknown", "reason": "form_unproved", "private": PRIVATE}, None, []]:
        result = m.safe_native_domain_observation(value, source=SOURCE, selection=SELECTION, table={},
                    document_sha256="a" * 64, page_generation="b" * 32, snapshot_id="c" * 32, observed_at=1)
        assert result["status"] == result["expanded_rendered_domain"] == "unknown"
        assert result["source_domain_complete"] is False and PRIVATE not in json.dumps(result)


@pytest.mark.asyncio
async def test_successful_existing_quote_keeps_additive_hashed_domain_receipt(tmp_path):
    m = load_driver(); session, store, _ = session_fixture(m, tmp_path)
    assert await session.collect() is True
    saved = next(iter(session.cursor["receipts"].values()))
    receipt = json.loads((tmp_path / saved["evidence"]["object_path"]).read_bytes())
    reference = receipt["native_domain_evidence"]
    raw = (tmp_path / reference["object_path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == reference["sha256"]
    assert json.loads(raw) == receipt["native_domain_observation"]
    # The synthetic pricing-only fixture intentionally lacks a real DOM/table
    # identity. Existing exact quote remains valid; domain stays unknown.
    assert receipt["native_domain_observation"]["status"] == "unknown"
    assert receipt["source_data_finalized"] is False and receipt["supplier_product_total_excluding_shipping"] == "11.26"
