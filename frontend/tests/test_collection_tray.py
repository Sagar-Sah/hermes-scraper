"""Collection delivery UI against a built app; all HTTP is locally intercepted.

No runtime server, supplier, source data, or real filesystem picker is used.
Build once to a temporary directory, or point HERMES_TRAY_DIST at a fresh build.
"""
import asyncio
import copy
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

FRONTEND = Path(__file__).resolve().parents[1]
ORIGIN = "https://hermes-fixture.invalid"
ZIP = b"PK\x03\x04synthetic collection fixture"


@pytest.fixture(scope="session")
def tray_dist(tmp_path_factory):
    supplied = os.environ.get("HERMES_TRAY_DIST")
    if supplied:
        target = Path(supplied).resolve()
        assert (target / "index.html").is_file()
        return target
    target = tmp_path_factory.mktemp("collection-ui-dist")
    subprocess.run(["npm", "run", "build", "--", "--outDir", str(target), "--emptyOutDir"],
                   cwd=FRONTEND, check=True, capture_output=True, text=True)
    return target


def product(identifier, **extra):
    result = dict(id=identifier, product_id=identifier, run_id="fixture-run", run_name="Synthetic harvest",
                  title=identifier, status="running", coverage_status="partial", row_count=3,
                  complete=False, exportable=True)
    result.update(extra)
    return result


PICKER = """() => {
  window.fixtureFiles = {events: [], bytes: 0, mode: 'ok'};
  window.showDirectoryPicker = async options => {
    const f=window.fixtureFiles; f.events.push('pick');
    if(f.mode==='cancel')throw new DOMException('Cancelled','AbortError');
    return {getFileHandle: async (name,options) => {f.events.push('handle:'+name); return {
      createWritable: async () => ({write: async part => {f.events.push('write');f.bytes+=part.byteLength;
        if(f.mode==='write_error')throw new Error('Fixture disk write failed');},
        close: async () => {f.events.push('close');if(f.mode==='close_error')throw new Error('Fixture close failed');},
        abort: async () => {f.events.push('abort');}})}}};
  };
}"""


@pytest_asyncio.fixture
async def tray(tray_dist):
    pw = pytest.importorskip("playwright.async_api")
    state = dict(items=[product("Complete card", complete=True, status="completed", coverage_status="complete", row_count=12),
                        product("Partial card", status="completed", row_count=999),
                        product("Known progress", progress=.4), product("Unknown progress")],
                 archives=[], pending_exports=[], exports=[], finalizes=[], downloads=[],
                 list_requests=0, list_error=False, export_error=False, download_error=False,
                 truncated=False, finalize_error=False, receipt_url=None, expected_token=None, cleanup_pending=False, finalize_lost=False, discards=[], list_delay=0, unexpected=[])
    async with pw.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium unavailable")
        browser = await runtime.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True, viewport={"width": 1400, "height": 1000})
        page = await context.new_page()
        await page.add_init_script("(" + PICKER + ")();")
        async def route(item):
            request = item.request
            parsed = urlsplit(request.url)
            if parsed.netloc == "fonts.googleapis.com":
                return await item.fulfill(body="", content_type="text/css")
            if parsed.netloc != "hermes-fixture.invalid":
                state["unexpected"].append(request.url)
                return await item.abort()
            path = parsed.path
            if path == "/api/v1/collection/products":
                state["list_requests"] += 1
                if state["list_error"]:
                    return await item.fulfill(status=503, json={"detail": "Fixture status unavailable"})
                snapshot = {k: copy.deepcopy(state[k]) for k in ("items", "archives", "pending_exports")}
                await asyncio.sleep(state["list_delay"])
                return await item.fulfill(json=snapshot)
            if path == "/api/v1/collection/exports" and request.method == "POST":
                body = request.post_data_json
                state["exports"].append(body)
                # Picker must run before export creation; no directory or machine path reaches the API.
                assert set(body) == {"product_ids", "allow_incomplete_product_ids"}
                if state["export_error"]:
                    return await item.fulfill(status=409, json={"detail": "Fixture export conflict"})
                receipt = dict(id="receipt-"+str(len(state["exports"])), filename="hermes-fixture.zip", size=len(ZIP), status="ready",
                    products=[copy.deepcopy(p) for p in state["items"] if p["id"] in body["product_ids"]])
                receipt["url"] = state["receipt_url"] or "/api/v1/collection/exports/"+receipt["id"]+"/download"
                state["pending_exports"].append(receipt)
                return await item.fulfill(json=receipt)
            if path.startswith("/api/v1/collection/") and path.endswith("/download"):
                state["downloads"].append(path)
                if state["expected_token"]: assert request.headers.get("authorization") == "Bearer " + state["expected_token"]
                if state["download_error"]:
                    return await item.fulfill(status=503, json={"detail": "Fixture download failure"})
                return await item.fulfill(body=ZIP[:-1] if state["truncated"] else ZIP, content_type="application/zip")
            if path.startswith("/api/v1/collection/exports/") and path.endswith("/discard"):
                state["discards"].append(request.post_data_json)
                assert request.post_data_json == {"discard": True}
                rid = path.split("/")[-2]
                state["pending_exports"] = [r for r in state["pending_exports"] if r["id"] != rid]
                return await item.fulfill(json={"id": rid, "status": "discarded"})
            if path.startswith("/api/v1/collection/exports/") and path.endswith("/finalize"):
                body = request.post_data_json
                state["finalizes"].append(body)
                if state["finalize_error"]:
                    return await item.fulfill(status=409, json={"detail": "Fixture product changed since export"})
                assert body["saved"] is True
                rid = path.split("/")[-2]
                receipt = next(r for r in state["pending_exports"] if r["id"] == rid)
                ids = {p["id"] for p in receipt["products"]}
                archived = [dict(id="archive-"+p["id"], product_id=p["id"], title=p["title"], filename="hermes-fixture.zip",
                             size=len(ZIP), download_url="/api/v1/collection/archives/archive-fixture/download")
                            for p in receipt["products"] if p["id"] in body["archive_product_ids"]]
                state["archives"].extend(a for a in archived if not any(old["id"] == a["id"] for old in state["archives"]))
                state["items"] = [p for p in state["items"] if p["id"] not in ids]
                if state["cleanup_pending"]:
                    receipt.update(status="finalized", finalize_selection=body["archive_product_ids"], cleanup={"status": "pending_private_cleanup"})
                else:
                    state["pending_exports"] = [r for r in state["pending_exports"] if r["id"] != rid]
                if state["finalize_lost"]: return await item.abort("failed")
                return await item.fulfill(json=dict(receipt_id=rid, status="finalized", archived=archived,
                    deleted_product_ids=sorted(ids-set(body["archive_product_ids"])),
                    cleanup={"status": "pending_private_cleanup" if state["cleanup_pending"] else "complete"}))
            if path == "/api/v1/health": return await item.fulfill(json={"authentication_required": False})
            if path == "/api/v1/runs": return await item.fulfill(json={"items": []})
            if path.startswith("/api/"):
                state["unexpected"].append(path)
                return await item.fulfill(status=404, json={"detail": "Unexpected fixture API"})
            target = tray_dist / (path.lstrip("/") or "index.html")
            assert target.resolve().is_relative_to(tray_dist.resolve())
            if not target.is_file(): return await item.fulfill(status=404)
            return await item.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target)[0] or "application/octet-stream")
        await page.route("**/*", route)
        await page.goto(ORIGIN)
        await pw.expect(page.get_by_role("button", name="Open collection", exact=True)).to_be_visible()
        for _ in range(100):
            if state["list_requests"]: break
            await asyncio.sleep(.01)
        yield page, state, pw.expect
        assert not state["unexpected"], state["unexpected"]
        await context.close()
        await browser.close()


async def open_tray(page):
    await page.get_by_role("button", name="Open collection", exact=True).click()
    return page.get_by_role("dialog", name="Your collection", exact=True)


async def refresh(page):
    await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


async def export_default(page):
    dialog = await open_tray(page)
    await dialog.get_by_role("button", name="Choose folder & export", exact=True).click()
    return page.get_by_role("dialog", name="Keep exported products?", exact=True)


@pytest.mark.asyncio
async def test_initial_default_completion_and_truthful_progress(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    await expect(dialog.get_by_role("checkbox", name="Select Complete card", exact=True)).to_be_checked()
    await expect(dialog.get_by_role("checkbox", name="Select Partial card", exact=True)).not_to_be_checked()
    await expect(dialog.get_by_text("Incomplete · Coverage Partial", exact=True)).to_be_visible()
    await expect(dialog.get_by_role("progressbar", name="Known progress collection progress", exact=True)).to_have_attribute("aria-valuenow", "40")
    assert await dialog.get_by_role("progressbar", name="Unknown progress collection progress", exact=True).get_attribute("aria-valuenow") is None
    await expect(page.locator(".collection-notification-count")).to_have_count(0)
    await expect(page.locator(".collection-box-arrival")).to_have_count(0)
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_completion_notification_respects_manual_selection_and_reduced_motion(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    await dialog.get_by_role("button", name="Select none", exact=True).click()
    await dialog.get_by_role("button", name="Close Your collection").click()
    state["items"][1].update(complete=True, coverage_status="complete")
    await refresh(page)
    await expect(page.locator(".collection-notification-count")).to_have_text("1")
    await expect(page.locator(".collection-box-arrival")).to_have_count(1)
    await page.emulate_media(reduced_motion="reduce")
    assert await page.locator(".collection-falling-package").evaluate("e=>getComputedStyle(e).animationName") == "none"
    dialog = await open_tray(page)
    await expect(dialog.get_by_role("checkbox", name="Select Partial card", exact=True)).not_to_be_checked()
    await dialog.get_by_role("button", name="Select all completed", exact=True).click()
    await expect(dialog.get_by_role("checkbox", name="Select Partial card", exact=True)).to_be_checked()
    await expect(page.locator(".collection-notification-count")).to_have_count(0)


@pytest.mark.asyncio
async def test_untouched_selection_adds_new_completion(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    state["items"][1].update(complete=True, coverage_status="complete")
    await dialog.get_by_role("button", name="Refresh collection").click()
    await expect(dialog.get_by_role("checkbox", name="Select Partial card", exact=True)).to_be_checked()


@pytest.mark.asyncio
async def test_unfinished_per_product_cancel_and_global_explicit_ack(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    await dialog.get_by_role("checkbox", name="Select Partial card", exact=True).click()
    ack = page.get_by_role("dialog", name="Include unfinished products?", exact=True)
    await ack.get_by_role("button", name="Keep selection unchanged").click()
    await expect(dialog.get_by_role("checkbox", name="Select Partial card", exact=True)).not_to_be_checked()
    await dialog.get_by_role("checkbox", name="Select Partial card", exact=True).click()
    await ack.get_by_role("checkbox", name="Select and acknowledge all unfinished").check()
    await ack.get_by_role("button", name="Include unfinished products", exact=True).click()
    await dialog.get_by_role("button", name="Choose folder & export").click()
    review = page.get_by_role("dialog", name="Keep exported products?", exact=True)
    await expect(review).to_be_visible()
    assert state["exports"] == [{"product_ids": [p["id"] for p in state["items"]],
        "allow_incomplete_product_ids": [p["id"] for p in state["items"] if not p["complete"]]}]
    assert state["finalizes"] == []
    assert await page.evaluate("fixtureFiles.events[0]") == "pick"


@pytest.mark.asyncio
async def test_folder_stream_closed_then_archive_only_after_explicit_confirmation(tray):
    page, state, expect = tray
    await page.evaluate("() => { Response.prototype.blob=async()=>{throw new Error('FSA must stream, not buffer the ZIP')}; }")
    review = await export_default(page)
    await expect(review).to_be_visible()
    assert await page.evaluate("fixtureFiles.events") == ["pick", "handle:hermes-fixture.zip", "write", "close"]
    assert await page.evaluate("fixtureFiles.bytes") == len(ZIP)
    await expect(review.get_by_role("checkbox", name="Archive Complete card")).to_be_checked()
    assert state["finalizes"] == []
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review).to_have_count(0)
    assert state["finalizes"] == [{"saved": True, "archive_product_ids": ["Complete card"]}]
    await expect(page.get_by_role("button", name="Download archive", exact=True)).to_be_visible()


@pytest.mark.asyncio
async def test_archive_selection_none_sends_explicit_removal(tray):
    page, state, expect = tray
    review = await export_default(page)
    await review.get_by_role("button", name="Select none", exact=True).click()
    await expect(review.get_by_role("checkbox", name="Archive Complete card")).not_to_be_checked()
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review).to_have_count(0)
    assert state["finalizes"] == [{"saved": True, "archive_product_ids": []}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "write_error", "close_error", "truncated", "download_error"])
async def test_failed_or_cancelled_save_never_runs_cleanup_and_keeps_selection(tray, mode):
    page, state, expect = tray
    await page.evaluate("mode=>fixtureFiles.mode=mode", mode)
    if mode in state: state[mode] = True
    dialog = await open_tray(page)
    await dialog.get_by_role("button", name="Choose folder & export").click()
    if mode == "cancel":
        await expect(dialog.get_by_text("Folder selection cancelled.", exact=False)).to_be_visible()
        assert state["exports"] == []
    else:
        await expect(dialog.get_by_role("alert")).to_be_visible()
        await expect(dialog.get_by_role("button", name="Download ZIP", exact=True)).to_be_visible()
        if mode in ("write_error", "close_error", "truncated"):
            assert "abort" in await page.evaluate("fixtureFiles.events")
    await expect(dialog.get_by_role("checkbox", name="Select Complete card", exact=True)).to_be_checked()
    await expect(page.get_by_role("dialog", name="Keep exported products?", exact=True)).to_have_count(0)
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_browser_download_requires_saved_ack_then_separate_archive_confirm(tray):
    page, state, expect = tray
    await page.evaluate("window.showDirectoryPicker=undefined")
    dialog = await open_tray(page)
    await dialog.get_by_role("button", name="Save ZIP…", exact=True).click()
    saved = page.get_by_role("dialog", name="Did you save the ZIP?", exact=True)
    await expect(saved).to_be_visible()
    assert state["finalizes"] == []
    await saved.get_by_role("button", name="Keep everything for now").click()
    assert state["finalizes"] == []
    await dialog.get_by_role("button", name="Download ZIP", exact=True).click()
    await saved.get_by_role("button", name="I saved the ZIP", exact=True).click()
    review = page.get_by_role("dialog", name="Keep exported products?", exact=True)
    await expect(review).to_be_visible()
    assert state["finalizes"] == []
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review).to_have_count(0)
    assert state["finalizes"][0]["saved"] is True


@pytest.mark.asyncio
async def test_finalization_conflict_keeps_dialog_and_exact_archive_selection(tray):
    page, state, expect = tray
    review = await export_default(page)
    await review.get_by_role("button", name="Select none", exact=True).click()
    state["finalize_error"] = True
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review.get_by_role("alert")).to_contain_text("Fixture product changed")
    await expect(review).to_be_visible()
    await expect(review.get_by_role("checkbox", name="Archive Complete card")).not_to_be_checked()
    assert len(state["items"]) == 4
    assert len(state["pending_exports"]) == 1
    state["finalize_error"] = False
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review).to_have_count(0)
    assert state["finalizes"] == [{"saved": True, "archive_product_ids": []}]*2


@pytest.mark.asyncio
async def test_pending_receipt_and_archive_survive_reload_and_download_without_cleanup(tray):
    page, state, expect = tray
    state["pending_exports"] = [dict(id="old",filename="interrupted.zip",url="/api/v1/collection/exports/old/download",
        size=len(ZIP),status="ready",products=[state["items"][0]])]
    state["archives"] = [dict(id="old-archive",title="Archived fixture",filename="archive.zip",size=len(ZIP),
        download_url="/api/v1/collection/archives/old-archive/download")]
    await page.reload()
    dialog = await open_tray(page)
    await expect(dialog.get_by_text("interrupted.zip", exact=True)).to_be_visible()
    await dialog.get_by_role("button", name="Download archive", exact=True).click()
    await expect(dialog.get_by_text("Archive saved to your selected folder.", exact=True)).to_be_visible()
    assert state["finalizes"] == []
    await dialog.get_by_role("button", name="Download ZIP", exact=True).click()
    await expect(page.get_by_role("dialog", name="Keep exported products?", exact=True)).to_be_visible()
    assert state["exports"] == []
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_poll_reconnect_retains_last_known_rows_and_action_errors(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    state["list_error"] = True
    await refresh(page)
    await expect(dialog.get_by_role("alert")).to_contain_text("Showing last-known collection.")
    await expect(dialog.get_by_role("checkbox", name="Select Complete card", exact=True)).to_be_checked()
    await expect(dialog.get_by_role("button", name="Choose folder & export")).to_be_disabled()
    state["list_error"] = False
    await refresh(page)
    await expect(dialog.get_by_role("alert")).to_have_count(0)
    state["export_error"] = True
    await dialog.get_by_role("button", name="Choose folder & export").click()
    await expect(dialog.get_by_role("alert")).to_contain_text("Fixture export conflict")
    await refresh(page)
    await expect(dialog.get_by_role("alert")).to_contain_text("Fixture export conflict")
    await expect(dialog.get_by_role("button", name="Choose folder & export")).to_be_enabled()
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_pending_cleanup_locks_selection_and_recovers_after_reload_without_download(tray):
    page, state, expect = tray
    review = await export_default(page)
    state["cleanup_pending"] = True
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review.get_by_role("alert")).to_contain_text("some files still need cleanup")
    await expect(review.get_by_role("checkbox", name="Archive Complete card")).to_be_disabled()
    await expect(review.get_by_role("button", name="Select none", exact=True)).to_be_disabled()
    await review.get_by_role("button", name="Close · retry later").click()
    await page.reload()
    dialog = await open_tray(page)
    await expect(dialog.get_by_role("button", name="Download ZIP", exact=True)).to_have_count(0)
    await expect(dialog.get_by_role("button", name="Discard ZIP", exact=True)).to_have_count(0)
    before = len(state["downloads"])
    await dialog.get_by_role("button", name="Retry cleanup", exact=True).click()
    await expect(review.get_by_role("checkbox", name="Archive Complete card")).to_be_disabled()
    state["cleanup_pending"] = False
    await review.get_by_role("button", name="Retry cleanup", exact=True).click()
    await expect(review).to_have_count(0)
    assert state["finalizes"] == [{"saved": True, "archive_product_ids": ["Complete card"]}]*2
    assert len(state["downloads"]) == before


@pytest.mark.asyncio
async def test_discard_server_zip_requires_confirmation_and_keeps_original_collection(tray):
    page, state, expect = tray
    review = await export_default(page)
    await review.get_by_role("button", name="Cancel · keep collection").click()
    dialog = page.get_by_role("dialog", name="Your collection", exact=True)
    await dialog.get_by_role("button", name="Discard ZIP", exact=True).click()
    discard = page.get_by_role("dialog", name="Discard server ZIP?", exact=True)
    await discard.get_by_role("button", name="Keep ZIP", exact=True).click()
    assert state["discards"] == []
    await dialog.get_by_role("button", name="Discard ZIP", exact=True).click()
    await discard.get_by_role("button", name="Discard server ZIP", exact=True).click()
    await expect(discard).to_have_count(0)
    await expect(dialog.get_by_role("button", name="Download ZIP", exact=True)).to_have_count(0)
    assert state["discards"] == [{"discard": True}]
    assert state["finalizes"] == []
    assert len(state["items"]) == 4
    await expect(dialog.get_by_role("checkbox", name="Select Complete card", exact=True)).to_be_checked()


@pytest.mark.asyncio
async def test_lost_finalize_response_does_not_leave_stale_local_download(tray):
    page, state, expect = tray
    review = await export_default(page)
    state["finalize_lost"] = True
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review.get_by_role("alert")).to_contain_text("Collection cleanup was not confirmed")
    await review.get_by_role("button", name="Cancel · keep collection").click()
    dialog = page.get_by_role("dialog", name="Your collection", exact=True)
    await dialog.get_by_role("button", name="Refresh collection", exact=True).click()
    await expect(dialog.get_by_role("button", name="Download ZIP", exact=True)).to_have_count(0)
    await expect(dialog.get_by_role("button", name="Download archive", exact=True)).to_be_visible()
    assert len(state["finalizes"]) == 1


@pytest.mark.asyncio
async def test_poll_started_before_export_does_not_erase_new_local_receipt(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    state["list_delay"] = .8
    before = state["list_requests"]
    await refresh(page)
    for _ in range(100):
        if state["list_requests"] > before: break
        await asyncio.sleep(.01)
    await dialog.get_by_role("button", name="Choose folder & export", exact=True).click()
    review = page.get_by_role("dialog", name="Keep exported products?", exact=True)
    await expect(review).to_be_visible()
    await review.get_by_role("button", name="Cancel · keep collection").click()
    await asyncio.sleep(.9)
    await expect(dialog.get_by_role("button", name="Download ZIP", exact=True)).to_be_visible()
    assert len(state["pending_exports"]) == 1
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_mobile_dialog_fits_viewport_and_esc_only_closes_without_cleanup(tray):
    page, state, expect = tray
    await page.set_viewport_size({"width": 390, "height": 844})
    dialog = await open_tray(page)
    await expect(dialog).to_be_visible()
    bounds = await dialog.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"]+bounds["width"] <= 390
    assert await dialog.evaluate("e=>e.scrollWidth<=e.clientWidth")
    await page.keyboard.press("Escape")
    await expect(dialog).to_have_count(0)
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_individual_unfinished_ack_does_not_include_other_unfinished_products(tray):
    page, state, expect = tray
    dialog = await open_tray(page)
    await dialog.get_by_role("checkbox", name="Select Partial card", exact=True).click()
    ack = page.get_by_role("dialog", name="Include unfinished products?", exact=True)
    await expect(ack.get_by_role("checkbox")).not_to_be_checked()
    await ack.get_by_role("button", name="Include unfinished products", exact=True).click()
    await expect(dialog.get_by_role("checkbox", name="Select Known progress", exact=True)).not_to_be_checked()
    await dialog.get_by_role("button", name="Choose folder & export", exact=True).click()
    await expect(page.get_by_role("dialog", name="Keep exported products?", exact=True)).to_be_visible()
    assert state["exports"] == [{"product_ids": ["Complete card", "Partial card"], "allow_incomplete_product_ids": ["Partial card"]}]
    assert state["finalizes"] == []


@pytest.mark.asyncio
async def test_archive_individual_selection_and_authenticated_zip_fetch(tray):
    page, state, expect = tray
    state["items"][1].update(complete=True, coverage_status="complete")
    await page.evaluate("sessionStorage.setItem('atlas_api_token','fixture-token')")
    state["expected_token"] = "fixture-token"
    await refresh(page)
    review = await export_default(page)
    await review.get_by_role("button", name="Select none", exact=True).click()
    await review.get_by_role("checkbox", name="Archive Partial card", exact=True).check()
    await review.get_by_role("button", name="Confirm archive / removal").click()
    await expect(review).to_have_count(0)
    assert state["finalizes"] == [{"saved": True, "archive_product_ids": ["Partial card"]}]
    assert [p["product_id"] for p in state["archives"]] == ["Partial card"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://external-fixture.invalid/zip", "/api/v1/other/download", "/api/v1/collection/exports/x/download#fragment", "https://name:password@hermes-fixture.invalid/api/v1/collection/exports/x/download"])
async def test_download_url_bound_to_same_workspace_without_credentials_or_fragment(tray, url):
    page, state, expect = tray
    state["receipt_url"] = url
    dialog = await open_tray(page)
    await dialog.get_by_role("button", name="Choose folder & export", exact=True).click()
    await expect(dialog.get_by_role("alert")).to_contain_text("must come from this Hermes workspace")
    assert state["downloads"] == []
    assert state["finalizes"] == []
    assert await page.evaluate("fixtureFiles.events") == ["pick"]
