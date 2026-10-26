"""Real temporary SQLite/local-file ZIP exports; no source or network access."""
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from crawler.config import Settings
from crawler.store import Batch, Event, Product, ProductRecordScope, Response, ResponseObservation, Run, Store, TrafficWindow
from crawler.collection_lifecycle import ExportReceipt, initialize_lifecycle
from crawler import collection_exports as exports


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    initialize_lifecycle(result)
    # Even a local cache miss must not invoke the network-capable object API.
    result.get_object = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Export fetched an object"))
    yield result
    result.engine.dispose()


def product(store, count=2, status="completed", coverage="complete", title="Saved Product"):
    with store.session() as session:
        run = Run(name="Local fixture", status="paused", spec={"adapter": "fixture", "profile_path": "/private/account"})
        session.add(run)
        session.flush()
        item = Product(run_id=run.id, url="https://supplier.test/product", title=title, status="running", epoch=1)
        session.add(item)
        session.flush()
        rid, pid = run.id, item.id
    rows = [{"id": f"row-{index}", "product_url": "https://supplier.test/product", "title": title,
             "configuration": {"Paper": {"value": "7", "label": "Matte"}, "Size": "24 x 24"},
             "quantity": index + 1, "currency": "USD", "subtotal": "20.00", "verification_status": "observed",
             "production": {"value": "1", "label": "3 Business Days"}, "evidence_ids": ["public-evidence"]}
            for index in range(count)]
    store.append_rows(pid, 1, rows)
    with store.session() as session:
        p = session.get(Product, pid)
        p.status, p.coverage_status = status, coverage
    return rid, pid


def request(*ids, incomplete=()):
    return {"product_ids": list(ids), "allow_incomplete_product_ids": list(incomplete)}


def bundle(store, result):
    return zipfile.ZipFile(exports.export_download_path(store, result["id"]))


def test_complete_export_has_all_rows_price_options_and_snapshot_receipt(store):
    _, pid = product(store, 3)
    result = exports.create_export_receipt(store, request(pid))
    assert result["status"] == "ready" and result["products"][0]["id"] == pid
    assert result["products"][0]["complete"] and result["products"][0]["exported_row_count"] == 3
    assert result["url"] == f"/api/v1/collection/exports/{result['id']}/download"
    with bundle(store, result) as archive:
        prefix = result["products"][0]["archive_prefix"]
        rows = [json.loads(line) for line in archive.read(prefix + "rows.jsonl").decode().splitlines()]
        assert len(rows) == 3 and rows[0]["configuration"]["Paper"]["label"] == "Matte"
        csv_rows = list(csv.DictReader(io.StringIO(archive.read(prefix + "rows.csv").decode())))
        assert len(csv_rows) == 3 and csv_rows[0]["subtotal"] == "20.00"
        assert prefix + "rows.parquet" in archive.namelist()
        meta = json.loads(archive.read(prefix + "metadata.json"))
        assert meta["scope"] == "all_saved_rows_at_batch_boundary" and meta["source_fetches"] == 0
    assert exports.read_export_receipt(store, result["id"]) == result
    assert exports.list_pending_exports(store)["items"] == [result]


@pytest.mark.parametrize("status,coverage", [("partial", "partial"), ("running", "complete"), ("completed", "unknown"), ("paused", "complete")])
def test_every_unfinished_product_needs_its_exact_acknowledgement(store, status, coverage):
    _, pid = product(store, status=status, coverage=coverage)
    with pytest.raises(ValueError, match="acknowledgement"):
        exports.create_export_receipt(store, request(pid))
    with store.session() as session:
        assert list(session.scalars(select(ExportReceipt))) == []
    result = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    assert result["products"][0]["requires_incomplete_ack"]
    assert result["products"][0]["incomplete_acknowledged"]
    assert any("INCOMPLETE PRODUCT" in note for note in result["products"][0]["warnings"])


def test_acknowledged_zero_rows_still_has_empty_files_metadata_and_images(store):
    _, pid = product(store, 0, status="partial", coverage="unknown")
    result = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    with bundle(store, result) as archive:
        prefix = result["products"][0]["archive_prefix"]
        assert archive.read(prefix + "rows.jsonl") == b""
        assert len(list(csv.reader(io.StringIO(archive.read(prefix + "rows.csv").decode())))) == 1
        assert prefix + "rows.parquet" in archive.namelist()
        assert json.loads(archive.read(prefix + "images.json"))["items"] == []


def test_snapshot_boundary_does_not_consume_rows_appended_after_capture(store, monkeypatch):
    _, pid = product(store, 1, status="running", coverage="unknown")
    original = exports.export_rows
    def append_then_export(rows, destination, **kwargs):
        store.append_rows(pid, 1, [{"id": "new-later", "product_url": "https://supplier.test/product", "quantity": 10,
            "currency": "USD", "subtotal": "30.00"}])
        return original(rows, destination, **kwargs)
    monkeypatch.setattr(exports, "export_rows", append_then_export)
    result = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    with bundle(store, result) as archive:
        prefix = result["products"][0]["archive_prefix"]
        assert len(archive.read(prefix + "rows.jsonl").splitlines()) == 1
    assert exports.product_snapshot(store, pid)["fingerprint"] != result["products"][0]["snapshot_fingerprint"]
    assert exports.list_collection_products(store)["items"][0]["row_count"] == 2


def test_all_legacy_rows_are_retained_with_scope_warning(store):
    _, pid = product(store, 3)
    with store.session() as session:
        session.add(ProductRecordScope(product_id=pid, schema_version=2, legacy_row_count=2))
    result = exports.create_export_receipt(store, request(pid))
    assert result["products"][0]["legacy_row_count"] == 2
    assert any("Legacy rows" in note for note in result["products"][0]["warnings"])
    with bundle(store, result) as archive:
        assert len(archive.read(result["products"][0]["archive_prefix"] + "rows.jsonl").splitlines()) == 3


def test_downloaded_images_missing_urls_and_safe_saved_artifacts(store):
    rid, pid = product(store)
    root = store.root / "exports" / rid / pid
    images = root / "images"
    images.mkdir(parents=True)
    data = b"\x89PNG\r\n\x1a\nsynthetic image"
    name = hashlib.sha256(data).hexdigest() + ".png"
    (images / name).write_bytes(data)
    missing = "a" * 64 + ".jpg"
    (images / "images.json").write_text(json.dumps([
        {"url": "https://supplier.test/image.png?token=IMAGE_SECRET", "file": name, "status": "downloaded"},
        {"url": "https://supplier.test/missing.jpg", "file": missing, "status": "downloaded"},
        {"url": "https://supplier.test/failed.jpg", "status": "unavailable"}]))
    (root / "options.json").write_text(json.dumps({"options": [{"label": "Matte"}], "cookie": "ARTIFACT_SECRET"}))
    (root / "response.html").write_text("PRIVATE_RAW_RESPONSE")
    result = exports.create_export_receipt(store, request(pid))
    prefix = result["products"][0]["archive_prefix"]
    with bundle(store, result) as archive:
        assert archive.read(prefix + "images/" + name) == data
        notes = json.loads(archive.read(prefix + "images.json"))["items"]
        assert {item["status"] for item in notes} == {"included", "missing_file", "unavailable"}
        assert json.loads(archive.read(prefix + "saved-artifacts/options.json")) == {"options": [{"label": "Matte"}]}
        assert prefix + "saved-artifacts/response.html" not in archive.namelist()
        meta = json.loads(archive.read(prefix + "metadata.json"))
        assert meta["excluded_saved_artifacts"] == [{"path": "response.html", "reason": "unsafe_or_rebuilt_format"}]
        assert b"IMAGE_SECRET" not in archive.read(prefix + "images.json")
        assert b"ARTIFACT_SECRET" not in archive.read(prefix + "saved-artifacts/options.json")


def test_image_urls_without_download_are_reported_without_fetch(store):
    _, pid = product(store, 0, status="running", coverage="unknown")
    store.append_rows(pid, 1, [{"id": "image-row", "product_url": "https://supplier.test/product", "quantity": 1,
        "currency": "USD", "subtotal": "10", "images": ["https://supplier.test/not-downloaded.png"]}])
    result = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    with bundle(store, result) as archive:
        notes = json.loads(archive.read(result["products"][0]["archive_prefix"] + "images.json"))["items"]
        assert notes == [{"source_url": "https://supplier.test/not-downloaded.png", "status": "not_downloaded", "reason": "no_saved_download"}]


def test_credentials_internal_paths_and_csv_formulas_are_removed_or_escaped(store):
    _, pid = product(store, 0, status="running", coverage="unknown", title="=HYPERLINK(\"bad\")")
    store.append_rows(pid, 1, [{"id": "secret-row", "product_url": "https://user:password@supplier.test/a?token=URL_SECRET&q=7#private",
        "title": "=1+1", "quantity": 1, "currency": "USD", "subtotal": "10.00",
        "configuration": {"Stock": "+COMMAND", "nested": {"password": "PASSWORD_SECRET", "label": "Matte"}},
        "pricing_context": {"cookie": "COOKIE_SECRET", "browserProfile": "/private/profile", "public": "kept"},
        "request_body": {"secret": "RAW_SECRET"}}])
    result = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    with bundle(store, result) as archive:
        prefix = result["products"][0]["archive_prefix"]
        text = archive.read(prefix + "rows.jsonl").decode()
        assert not any(value in text for value in ["URL_SECRET", "PASSWORD_SECRET", "COOKIE_SECRET", "RAW_SECRET", "/private/profile", "user:password"])
        row = json.loads(text)
        assert row["product_url"] == "https://supplier.test/a?q=7"
        assert row["pricing_context"] == {"public": "kept"}
        cells = list(csv.DictReader(io.StringIO(archive.read(prefix + "rows.csv").decode())))[0]
        assert cells["title"] == "'=1+1" and cells["attr:Stock"] == "'+COMMAND"
        assert row["title"] == "=1+1"


def test_standard_form_csrf_session_and_signed_url_credentials_are_redacted():
    names = ["form_key", "formKey", "csrf", "csrf_token", "csrftoken", "xsrf", "nonce", "session",
             "sessionId", "PHPSESSID", "sid", "auth", "jwt", "sig", "signature", "X-Amz-Signature"]
    result = exports.sanitize_public({**{name: "SECRET" for name in names}, "source_key": "public", "Stock": "Matte"})
    assert result == {"source_key": "public", "Stock": "Matte"}
    url = "https://name:password@supplier.test/a?form_key=SECRET&csrf=SECRET&PHPSESSID=SECRET&signature=SECRET&size=7"
    assert exports.sanitize_public(url) == "https://supplier.test/a?size=7"


def test_two_selected_products_with_same_url_have_separate_folders(store):
    _, a = product(store, 1)
    _, b = product(store, 2)
    result = exports.create_export_receipt(store, request(a, b))
    assert {p["id"] for p in result["products"]} == {a, b}
    assert len({p["archive_prefix"] for p in result["products"]}) == 2
    with bundle(store, result) as archive:
        for item in result["products"]:
            assert len(archive.read(item["archive_prefix"] + "rows.jsonl").splitlines()) == item["row_count"]


def test_snapshot_fences_new_observations_events_traffic_and_artifacts(store):
    rid, pid = product(store)
    previous = exports.product_snapshot(store, pid)["fingerprint"]
    with store.session() as session:
        response = Response(id="response1", url="https://supplier.test/price", raw_hash="a" * 64, semantic_hash="b" * 64)
        session.add(response)
        session.add(ResponseObservation(id="observation1", run_id=rid, product_id=pid, response_id="response1", kind="quote", context={"quantity": 1}))
        session.add(Event(run_id=rid, type="quote", payload={"product_id": pid, "count": 1}))
        session.add(Event(run_id=rid, type="other-product", payload={"product_id": "f" * 32}))
        session.add(Event(run_id=rid, type="run-only", payload={"count": 9}))
        session.add(TrafficWindow(product_id=pid, run_id=rid, requests_start=1, cache_hits_start=0, rows_start=0))
    current = exports.product_snapshot(store, pid)
    assert current["fingerprint"] != previous and len(current["events"]) == len(current["response_observations"]) == 1
    root = store.root / "exports" / rid / pid
    root.mkdir(parents=True)
    (root / "options.json").write_text('{"options":[1]}')
    assert exports.product_snapshot(store, pid)["fingerprint"] != current["fingerprint"]


@pytest.mark.parametrize("kind", ["batch", "image", "export_directory"])
def test_symlinks_never_export_external_content(store, tmp_path, kind):
    rid, pid = product(store)
    outside = tmp_path / "outside"
    outside.write_text("EXTERNAL_PRIVATE")
    if kind == "batch":
        with store.session() as session:
            row = session.scalar(select(Batch).where(Batch.product_id == pid))
            path = store.root / row.object_path
        path.unlink()
        path.symlink_to(outside)
    elif kind == "image":
        path = store.root / "exports" / rid / pid / "images"
        path.mkdir(parents=True)
        (path / ("a" * 64 + ".png")).symlink_to(outside)
    else:
        path = store.root / "exports" / rid / pid
        path.parent.mkdir(parents=True)
        path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        exports.create_export_receipt(store, request(pid))
    with store.session() as session:
        assert list(session.scalars(select(ExportReceipt))) == []


def test_corrupted_saved_batch_or_bundle_is_rejected(store):
    _, pid = product(store)
    result = exports.create_export_receipt(store, request(pid))
    path = exports.export_download_path(store, result["id"])
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed"):
        exports.export_download_path(store, result["id"])
    _, pid = product(store)
    with store.session() as session:
        row = session.scalar(select(Batch).where(Batch.product_id == pid))
        path = store.root / row.object_path
    path.write_bytes(b"bad saved body")
    with pytest.raises(ValueError, match="hash changed"):
        exports.create_export_receipt(store, request(pid))


def test_identical_pending_export_is_reused_but_overlapping_fresh_export_is_allowed(store):
    _, a = product(store, 1)
    _, b = product(store, 2)
    first = exports.create_export_receipt(store, request(a))
    assert exports.create_export_receipt(store, request(a)) == first
    second = exports.create_export_receipt(store, request(a, b))
    assert second["id"] != first["id"]
    with store.session() as session:
        assert len(list(session.scalars(select(ExportReceipt)))) == 2


def test_new_rows_after_active_export_allow_fresh_export_and_safe_old_zip_discard(store):
    _, pid = product(store, 1, status="running", coverage="unknown")
    old = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    old_path = exports.export_download_path(store, old["id"])
    store.append_rows(pid, 1, [{"id": "later", "product_url": "https://supplier.test/product", "quantity": 9,
        "currency": "USD", "subtotal": "30.00"}])
    new = exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    assert new["id"] != old["id"] and new["products"][0]["row_count"] == 2
    before = exports.product_snapshot(store, pid)
    assert exports.discard_export(store, old["id"]) == {"id": old["id"], "status": "discarded", "products_unchanged": True}
    assert not old_path.exists() and exports.export_download_path(store, new["id"]).exists()
    assert exports.product_snapshot(store, pid) == before
    assert exports.discard_export(store, old["id"])["status"] == "discarded"
    assert [r["id"] for r in exports.list_pending_exports(store)["items"]] == [new["id"]]


def test_finalized_pending_cleanup_is_recoverable_without_private_paths(store):
    _, pid = product(store)
    result = exports.create_export_receipt(store, request(pid))
    with store.session() as session:
        receipt = session.get(ExportReceipt, result["id"])
        receipt.status = "finalized"
        receipt.finalize_selection = [pid]
        receipt.result = {"cleanup": {"status": "pending_private_cleanup", "deleted_private_files": 2,
            "deleted_private_bytes": 9, "pending_private_files": [{"path": "exports/PRIVATE_PATH"}],
            "private_directories": ["exports/PRIVATE_DIR"], "pending_export_files": []}}
    public = exports.read_export_receipt(store, result["id"])
    assert public["status"] == "finalized" and public["finalize_selection"] == [pid]
    assert public["cleanup"]["pending_private_file_count"] == 1
    assert public["cleanup"]["deleted_private_files"] == 2
    assert "PRIVATE" not in json.dumps(public)
    assert exports.list_pending_exports(store)["items"] == [public]
    with pytest.raises(ValueError, match="Only pending"):
        exports.discard_export(store, result["id"])
    with store.session() as session:
        receipt = session.get(ExportReceipt, result["id"])
        receipt.result = {"cleanup": {"status": "complete"}}
    assert exports.list_pending_exports(store)["items"] == []


def test_saved_csv_secret_columns_are_excluded_and_nested_json_sanitized(store):
    rid, pid = product(store)
    root = store.root / "exports" / rid / pid
    root.mkdir(parents=True)
    with (root / "options.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["Label", "password", "Details"])
        writer.writerow(["=FORMULA", "CSV_SECRET", '{"stock":"matte","cookie":"JSON_SECRET"}'])
    result = exports.create_export_receipt(store, request(pid))
    with bundle(store, result) as archive:
        text = archive.read(result["products"][0]["archive_prefix"] + "saved-artifacts/options.csv").decode()
        assert "SECRET" not in text
        assert list(csv.reader(io.StringIO(text))) == [["Label", "Details"], ["'=FORMULA", '{"stock":"matte"}']]


@pytest.mark.parametrize("bad", [None, {}, {"product_ids": [], "allow_incomplete_product_ids": []},
    {"product_ids": [[]], "allow_incomplete_product_ids": []},
    {"product_ids": ["../escape"], "allow_incomplete_product_ids": []}])
def test_invalid_export_requests_are_value_errors(store, bad):
    with pytest.raises(ValueError):
        exports.create_export_receipt(store, bad)


def test_selection_requires_existing_unique_products_and_exact_ack_set(store):
    _, pid = product(store)
    with pytest.raises(ValueError):
        exports.create_export_receipt(store, request(pid, pid))
    with pytest.raises(ValueError):
        exports.create_export_receipt(store, request(pid, incomplete=[pid]))
    with pytest.raises(LookupError):
        exports.create_export_receipt(store, request("f" * 32))
