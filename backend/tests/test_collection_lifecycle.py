"""Lifecycle integration uses only temporary SQLite/files and synthetic rows."""
import gzip
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, select

from crawler.config import Settings
from crawler.store import (Batch, BrowserBranch, Event, Product, ProductRecordScope,
                           Response, ResponseObservation, Run, Store, canonical)
from crawler.collection_exports import create_export_receipt
from crawler.collection_lifecycle import (ExportReceipt, LifecycleError, ProductArchive,
    archive_download_path, finalize_export, list_archives)
import crawler.collection_lifecycle as lifecycle


@pytest.fixture
def collection(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = Store(Settings(data_dir=tmp_path / "state", database_url="sqlite:///" + str(tmp_path / "fixture.db"), s3_bucket=""))
    with store.session() as session:
        run = Run(id=uuid.uuid4().hex, name="Synthetic harvest", status="paused", spec={"adapter": "fixture"})
        session.add(run)
        session.flush()
        rid = run.id
    ids, objects = [], []
    for number in range(3):
        pid, row_id = uuid.uuid4().hex, uuid.uuid4().hex
        row = {"id": row_id, "product_url": "https://catalog.example/product-" + str(number),
               "title": "Synthetic " + str(number), "configuration": {"size": "Small"},
               "quantity": 10, "currency": "USD", "subtotal": "12.50", "unit_price": "1.25",
               "verification_status": "source_observed", "images": [], "production": {"min_days": 2, "max_days": 3}}
        object_path = store.put_object(gzip.compress((canonical(row) + "\n").encode(), mtime=0), "jsonl.gz")
        branch_object = store.put_object(gzip.compress(canonical([row_id]).encode(), mtime=0), "branch-rows.json.gz")
        with store.session() as session:
            session.add(Product(id=pid, run_id=rid, url=row["product_url"], title=row["title"], status="paused",
                row_count=1, coverage_status="partial", epoch=2, export_manifest={}))
            session.add(Batch(id=uuid.uuid4().hex, product_id=pid, object_path=object_path, row_count=1, keys=[row_id], sequence=0))
            session.add(BrowserBranch(id=uuid.uuid4().hex, run_id=rid, product_id=pid, scope="fixture",
                prefix={"size": "Small"}, rows_object=branch_object, row_count=1, committed_row_count=1, details={}))
            session.add(ProductRecordScope(product_id=pid, schema_version=2, legacy_row_count=0))
            session.add(Event(run_id=rid, type="progress", payload={"product_id": pid, "rows": 1}))
        folder = store.root / "exports" / rid / pid
        folder.mkdir(parents=True)
        (folder / "saved.json").write_text('{"label":"synthetic"}')
        ids.append(pid)
        objects += [object_path, branch_object]
    catalog = store.root / "exports" / rid / "catalog"
    catalog.mkdir()
    (catalog / "combined.csv").write_text("old combined derived rows")
    try:
        yield store, rid, ids, objects
    finally:
        store.engine.dispose()


def exported(collection, ids=None):
    store, _, products, _ = collection
    chosen = products[:2] if ids is None else ids
    return create_export_receipt(store, {"product_ids": chosen, "allow_incomplete_product_ids": chosen})


def retained(store, ids):
    with store.session() as session:
        return [session.get(Product, identifier) is not None for identifier in ids]


def test_saved_export_archives_checked_deletes_unchecked_and_preserves_others(collection):
    store, rid, ids, objects = collection
    receipt = exported(collection)
    bundle = store.root / "collection-exports" / receipt["id"] / "collection.zip"
    result = finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert result["archived_product_ids"] == [ids[0]] and result["deleted_product_ids"] == [ids[1]]
    assert retained(store, ids) == [False, False, True]
    assert all((store.root / key).is_file() for key in objects)  # Shared object lifetime is not guessed.
    assert (store.root / "exports" / rid / ids[2] / "saved.json").is_file()
    assert not (store.root / "exports" / rid / ids[0]).exists()
    assert not (store.root / "exports" / rid / ids[1]).exists()
    assert not (store.root / "exports" / rid / "catalog").exists()
    assert not bundle.exists()
    assert result["cleanup"]["deleted_private_files"] == 3
    assert result["cleanup"]["deleted_export_files"] == 2 and result["cleanup"]["status"] == "complete"
    archives = list_archives(store)
    assert len(archives) == 1 and archives[0]["product_id"] == ids[0] and archives[0]["complete"] is False
    path = archive_download_path(store, archives[0]["id"])
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert "archive.json" in names and any(name.endswith("rows.parquet") for name in names)
        assert all(ids[1] not in name and ids[2] not in name for name in names)
        metadata = json.loads(archive.read("archive.json"))
        assert metadata["product"]["product_id"] == ids[0]
        assert metadata["timing"]["recorded_at"] > 0
        assert "server_now" not in metadata["timing"] and "session" not in metadata["timing"]
    with store.session() as session:
        assert session.get(Run, rid) is not None
        assert list(session.scalars(select(Batch.product_id))) == [ids[2]]
        from crawler.store import ProductTiming
        assert {row.product_id for row in session.scalars(select(ProductTiming))} == {ids[2]}


@pytest.mark.parametrize("saved", [False, None, 1, "true"])
def test_explicit_boolean_saved_ack_required(collection, saved):
    store, _, ids, _ = collection
    receipt = exported(collection)
    with pytest.raises(LifecycleError, match="saved"):
        finalize_export(store, receipt["id"], ids[:2], saved=saved)
    assert retained(store, ids) == [True, True, True] and list_archives(store) == []


def test_idempotent_retry_has_same_archives_and_refuses_new_selection(collection):
    store, _, ids, _ = collection
    receipt = exported(collection)
    first = finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert finalize_export(store, receipt["id"], list(reversed(ids[:2])), saved=True) == first
    with pytest.raises(LifecycleError, match="different archive selection"):
        finalize_export(store, receipt["id"], [], saved=True)
    assert len(list_archives(store)) == 2


@pytest.mark.parametrize("change", ["new_rows", "new_event", "new_observation", "private_file", "status", "epoch"])
def test_changed_snapshot_holds_without_any_product_deletion(collection, change):
    store, rid, ids, _ = collection
    receipt = exported(collection)
    with store.session() as session:
        product = session.get(Product, ids[0])
        if change == "new_rows":
            product.row_count += 1
            session.add(Batch(id=uuid.uuid4().hex, product_id=ids[0], object_path="objects/not-read", row_count=1, keys=["new"], sequence=1))
        if change == "new_event":session.add(Event(run_id=rid, type="source", payload={"product_id": ids[0], "new": True}))
        if change == "new_observation":session.add(ResponseObservation(id=uuid.uuid4().hex, run_id=rid, product_id=ids[0], response_id="new", kind="source", request_signature="new", context={}))
        if change == "status":product.status = "running"
        if change == "epoch":product.epoch += 1
    if change == "private_file":(store.root / "exports" / rid / ids[0] / "new.json").write_text("{}")
    with pytest.raises((LifecycleError, ValueError), match="changed|Pause active"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert retained(store, ids) == [True, True, True] and list_archives(store) == []


@pytest.mark.parametrize("state", ["queued", "running", "discovering", "exporting"])
def test_active_run_rejected_even_if_selected_product_paused(collection, state):
    store, rid, ids, _ = collection
    with store.session() as session:session.get(Run, rid).status = state
    receipt = exported(collection)
    with pytest.raises(LifecycleError, match="Pause the harvest"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert retained(store, ids) == [True, True, True]


@pytest.mark.parametrize("kind", ["campaign", "automation", "owned_run", "probe_job", "unsafe_index", "finalization"])
def test_protected_references_refuse_all_cleanup(collection, kind):
    store, rid, ids, _ = collection
    if kind == "owned_run":
        with store.session() as session:session.get(Run, rid).spec = {"source_metadata": {"automation_owner": "existing"}}
    receipt = exported(collection)
    if kind in {"campaign", "automation", "unsafe_index", "finalization"}:
        folder = store.root / ("campaign-automation" if kind == "automation" else "finalizations" if kind == "finalization" else "campaigns")
        folder.mkdir()
        (folder / "protected.json").write_text("{" if kind == "unsafe_index" else json.dumps({"products": {ids[0]: {"run_id": rid}}}))
    if kind == "probe_job":
        from crawler.store import ProbeJob
        with store.session() as session:session.add(ProbeJob(id=uuid.uuid4().hex, run_id=rid, budget=1, status="completed"))
    with pytest.raises(LifecycleError, match="referenced|association|protected"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert retained(store, ids) == [True, True, True]


def test_bundle_hash_or_archive_storage_failure_never_deletes_products(collection, monkeypatch):
    store, _, ids, _ = collection
    receipt = exported(collection)
    bundle = store.root / "collection-exports" / receipt["id"] / "collection.zip"
    original = bundle.read_bytes()
    bundle.write_bytes(original + b"corrupt")
    with pytest.raises(LifecycleError, match="changed"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    bundle.write_bytes(original)
    def fail(*args, **kwargs):raise OSError("synthetic full disk")
    monkeypatch.setattr(lifecycle.tempfile, "mkstemp", fail)
    with pytest.raises((LifecycleError, OSError)):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert retained(store, ids) == [True, True, True] and list_archives(store) == []


def test_second_archive_failure_rolls_back_all_db_deletes(collection, monkeypatch):
    store, _, ids, _ = collection
    receipt = exported(collection)
    original, calls = lifecycle._stage_archive, []
    def stage(*args):
        calls.append(1)
        if len(calls) == 2:raise LifecycleError("synthetic archive failure")
        return original(*args)
    monkeypatch.setattr(lifecycle, "_stage_archive", stage)
    with pytest.raises(LifecycleError, match="synthetic"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert retained(store, ids) == [True, True, True] and list_archives(store) == []
    with store.session() as session:assert session.get(ExportReceipt, receipt["id"]).status == "ready"


def test_only_exported_ids_allowed_and_duplicate_selection_rejected(collection):
    store, _, ids, _ = collection
    receipt = exported(collection)
    for selected in ([ids[2]], [ids[0], ids[0]], ["../escape"]):
        with pytest.raises(LifecycleError):finalize_export(store, receipt["id"], selected, saved=True)
    assert retained(store, ids) == [True, True, True]


def test_cleanup_interruption_is_durable_retry_without_redeletion(collection, monkeypatch):
    store, rid, ids, _ = collection
    receipt = exported(collection)
    target = store.root / "exports" / rid / ids[0] / "saved.json"
    unlink = Path.unlink
    def blocked(path, *args, **kwargs):
        if path == target:raise PermissionError("synthetic private cleanup failure")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", blocked)
        result = finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert result["cleanup"]["status"] == "pending_private_cleanup" and target.is_file()
    assert retained(store, ids) == [False, False, True]
    retry = finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert retry["cleanup"]["status"] == "complete" and not target.exists() and len(list_archives(store)) == 1


def test_symlink_private_tree_or_bundle_is_never_deleted(collection, tmp_path):
    store, rid, ids, _ = collection
    receipt = exported(collection)
    private = store.root / "exports" / rid / ids[0] / "saved.json"
    private.unlink()
    other = tmp_path / "must-remain.json"
    other.write_text("{}")
    private.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        finalize_export(store, receipt["id"], ids[:2], saved=True)
    assert other.read_text() == "{}" and retained(store, ids) == [True, True, True]


def test_archived_metadata_sanitized_and_download_verifies_bytes(collection):
    store, _, ids, _ = collection
    with store.session() as session:
        session.get(Product, ids[0]).url = "https://user:password@catalog.example/p?token=synthetic-secret&size=1"
    receipt = exported(collection, [ids[0]])
    result = finalize_export(store, receipt["id"], [ids[0]], saved=True)
    archive = result["archived"][0]
    path = archive_download_path(store, archive["id"])
    with zipfile.ZipFile(path) as source:
        metadata = source.read("archive.json").decode()
        assert "synthetic-secret" not in metadata and "user:password" not in metadata and "size=1" in metadata
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(LifecycleError, match="changed"):archive_download_path(store, archive["id"])


def test_two_concurrent_finalizers_return_one_durable_result(collection):
    from concurrent.futures import ThreadPoolExecutor
    store, _, ids, _ = collection
    receipt = exported(collection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finalize_export, store, receipt["id"], [ids[0]], saved=True) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1] and len(list_archives(store)) == 1
    assert retained(store, ids) == [False, False, True]


def test_no_archive_selection_deletes_only_acknowledged_receipt_products(collection):
    store, _, ids, _ = collection
    receipt = exported(collection)
    result = finalize_export(store, receipt["id"], [], saved=True)
    assert result["archived"] == [] and result["deleted_product_ids"] == sorted(ids[:2])
    assert list_archives(store) == [] and retained(store, ids) == [False, False, True]


def test_new_private_data_or_protection_during_archive_stage_holds(collection, monkeypatch):
    store, rid, ids, _ = collection
    receipt = exported(collection)
    original = lifecycle._stage_archive
    def stage(*args):
        result = original(*args)
        (store.root / "exports" / rid / ids[0] / "new.json").write_text('{"new":true}')
        return result
    monkeypatch.setattr(lifecycle, "_stage_archive", stage)
    with pytest.raises(LifecycleError, match="changed while archiving"):
        finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert retained(store, ids) == [True, True, True] and list_archives(store) == []


def test_unknown_coordinator_product_reference_holds(collection):
    store, _, ids, _ = collection
    metadata = MetaData()
    table = Table("future_coordinator", metadata, Column("id", String, primary_key=True), Column("product_id", String))
    metadata.create_all(store.engine)
    with store.engine.begin() as connection:connection.execute(insert(table).values(id="other", product_id=ids[0]))
    receipt = exported(collection)
    with pytest.raises(LifecycleError, match="another durable coordinator"):
        finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert retained(store, ids) == [True, True, True]


def test_archive_member_identity_conflict_and_path_escape_hold(collection):
    store, _, ids, _ = collection
    receipt = exported(collection)
    with store.session() as session:
        record = session.get(ExportReceipt, receipt["id"])
        bad = json.loads(json.dumps(record.manifest))
        bad["products"][0]["archive_prefix"] = "products/../escape/"
        record.manifest = bad
    with pytest.raises(LifecycleError, match="path is unsafe"):
        finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert retained(store, ids) == [True, True, True]


def test_archive_complete_flag_comes_from_exact_snapshot_status_and_coverage(collection):
    store, _, ids, _ = collection
    with store.session() as session:
        product = session.get(Product, ids[0])
        product.status, product.coverage_status = "completed", "complete"
    receipt = create_export_receipt(store, {"product_ids": [ids[0]], "allow_incomplete_product_ids": []})
    result = finalize_export(store, receipt["id"], [ids[0]], saved=True)
    assert result["archived"][0]["complete"] is True
    assert list_archives(store)[0]["complete"] is True
