"""Explicit saved-export finalization for a single trusted Hermes workspace.

The export receipt is a snapshot, not permission to remove a changing product.
Archive bytes are committed before the DB transaction removes any product.
Shared response/content-addressed objects are deliberately retained: this is
product cleanup, not a universal garbage collector or a campaign mutation API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from sqlalchemy import Column, Float, Integer, JSON, String, delete, inspect, select

from .store import (Base, Batch, BrowserBranch, Event, Product, ProductRecordScope,
                    ResponseObservation, Run, TrafficWindow, canonical)

_SCHEMA_LOCK = threading.Lock()


class ExportReceipt(Base):
    __tablename__ = "collection_export_receipts"
    id = Column(String, primary_key=True)
    created_at = Column(Float, nullable=False, default=time.time)
    status = Column(String, nullable=False, default="ready")
    bundle_path = Column(String, nullable=False)
    bundle_sha256 = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    manifest = Column(JSON, nullable=False)
    finalized_at = Column(Float, nullable=True)
    finalize_selection = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)


class ProductArchive(Base):
    __tablename__ = "collection_product_archives"
    id = Column(String, primary_key=True)
    receipt_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=False, index=True)
    created_at = Column(Float, nullable=False, default=time.time)
    title = Column(String, nullable=False, default="")
    bundle_path = Column(String, nullable=False)
    bundle_sha256 = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    metadata_json = Column(JSON, nullable=False)


class LifecycleError(ValueError):
    def __init__(self, detail, status_code=409):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def initialize_lifecycle(store):
    """Create only these two additive tables; no existing schema is migrated."""
    with _SCHEMA_LOCK:
        ExportReceipt.__table__.create(store.engine, checkfirst=True)
        ProductArchive.__table__.create(store.engine, checkfirst=True)


def _id(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise LifecycleError("Invalid collection identifier", 422)
    return value


def _relative(value):
    if (type(value) is not str or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise LifecycleError("Collection artifact path is unsafe")
    return PurePosixPath(value)


def _path(store, relative, namespace=None, *, missing=False):
    rel = _relative(relative)
    if namespace is not None and rel.parts[0] != namespace:
        raise LifecycleError("Collection artifact is outside its private directory")
    root = Path(store.root).resolve()
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                continue
            raise LifecycleError("Collection artifact is missing", 404) from None
        if stat.S_ISLNK(mode):
            raise LifecycleError("Collection artifact symlinks are not permitted")
    if not target.resolve().is_relative_to(root):
        raise LifecycleError("Collection artifact escaped its private directory")
    return target


def _sha(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _verified_file(store, relative, expected, size, namespace):
    if type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected) or type(size) is not int or size < 0:
        raise LifecycleError("Collection artifact identity is invalid")
    path = _path(store, relative, namespace)
    if not path.is_file() or path.stat().st_size != size or _sha(path) != expected:
        raise LifecycleError("Collection artifact changed; export again before cleanup")
    return path


def _products(receipt):
    manifest = receipt.manifest
    products = manifest.get("products") if isinstance(manifest, dict) else None
    if not isinstance(products, list) or not products or len(products) > 10000:
        raise LifecycleError("Export product manifest is invalid")
    result, prefixes = {}, set()
    for product in products:
        if not isinstance(product, dict):
            raise LifecycleError("Export product snapshot is invalid")
        identifier = _id(product.get("product_id"))
        prefix = product.get("archive_prefix")
        if (type(prefix) is not str or not prefix.endswith("/")
                or len(_relative(prefix[:-1]).parts) != 2 or not prefix.startswith("products/")
                or prefix in prefixes):
            raise LifecycleError("Export product archive prefixes are invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(product.get("fingerprint", ""))):
            raise LifecycleError("Export product snapshot fingerprint is invalid")
        if identifier in result:
            raise LifecycleError("Export product manifest contains duplicate identities")
        result[identifier] = product
        prefixes.add(prefix)
    return result


def _references(value, product_id, run_id):
    if isinstance(value, dict):
        return any(k in {product_id, run_id} or _references(v, product_id, run_id)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_references(v, product_id, run_id) for v in value)
    return type(value) is str and value in {product_id, run_id}


def _protected(store, session, product):
    """Refuse known persistent external owners instead of leaving broken links."""
    run = session.get(Run, product.run_id)
    metadata = (run.spec or {}).get("source_metadata", {}) if run else {}
    if isinstance(metadata, dict) and (metadata.get("automation_owner") or metadata.get("automation_association")):
        raise LifecycleError("Product belongs to an automation-owned Run; retain it until that association is explicitly retired")
    # These are live durable controllers, not export indexes. Even a completed
    # controller may rely on retained product IDs/evidence for later inspection.
    for name in ("campaigns", "campaign-automation", "printograph-owned-browser-runs", "finalizations"):
        directory = _path(store, name, missing=True)
        if not directory.exists():
            continue
        if any(p.is_symlink() for p in directory.rglob("*")):
            raise LifecycleError("A protected collection index contains a symlink; retain the product")
        for path in directory.rglob("*.json"):
            relative = path.relative_to(Path(store.root).resolve()).as_posix()
            path = _path(store, relative)
            if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                raise LifecycleError("A protected collection index cannot be verified; retain the product")
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                raise LifecycleError("A protected collection index cannot be verified; retain the product") from None
            if _references(value, product.id, product.run_id):
                raise LifecycleError("Product is referenced by " + name + "; retire its association before cleanup")
    # Optional installed coordinators can add tables. Never guess how to remove
    # their state; any run/product reference outside the known cleanup set holds.
    allowed = {"runs", "products", "batches", "browser_branches", "product_record_scopes",
               "traffic_windows", "response_observations", "events", "collection_export_receipts",
               "collection_product_archives", "responses", "profiles", "origins", "permits", "request_leases"}
    connection = session.connection()
    catalog = inspect(connection)
    from sqlalchemy import MetaData, Table
    for table_name in catalog.get_table_names():
        if table_name in allowed:
            continue
        columns = {c["name"] for c in catalog.get_columns(table_name)}
        guards = []
        table = None
        for key, value in (("product_id", product.id), ("run_id", product.run_id)):
            if key in columns:
                if table is None:
                    table = Table(table_name, MetaData(), autoload_with=connection)
                guards.append(table.c[key] == value)
        if guards:
            from sqlalchemy import or_
            if connection.execute(select(table).where(or_(*guards)).limit(1)).first() is not None:
                raise LifecycleError("Product is referenced by another durable coordinator; retain it until that work is retired")


def _file_index(receipt):
    entries = receipt.manifest.get("files")
    if not isinstance(entries, list):
        raise LifecycleError("Export file index is invalid")
    index = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise LifecycleError("Export file index is invalid")
        name = _relative(entry.get("path")).as_posix()
        if name in index or type(entry.get("size")) is not int or entry["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise LifecycleError("Export file identity is invalid")
        index[name] = entry
    return index


def _stage_archive(store, receipt, snapshot, bundle):
    product_id = snapshot["product_id"]
    archive_id = hashlib.sha256((receipt.id + ":" + product_id).encode()).hexdigest()
    prefix = snapshot.get("archive_prefix")
    if type(prefix) is not str or not prefix.endswith("/"):
        raise LifecycleError("Export product archive prefix is invalid")
    _relative(prefix[:-1])
    if not prefix.startswith("products/"):
        raise LifecycleError("Export product archive prefix is outside its product directory")
    index = _file_index(receipt)
    members = {name: value for name, value in index.items() if name.startswith(prefix)}
    if not members:
        raise LifecycleError("Export product contains no indexed archive files")
    directory = _path(store, "collection-archives", missing=True)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = _path(store, "collection-archives/" + archive_id + ".zip", missing=True)
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=directory)
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as output:
            with zipfile.ZipFile(bundle) as source:
                infos = source.infolist()
                if len({i.filename for i in infos}) != len(infos):
                    raise LifecycleError("Export bundle contains duplicate members")
                available = {i.filename: i for i in infos}
                for name in sorted(members):
                    info = available.get(name)
                    expected = members[name]
                    if (info is None or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)
                            or info.file_size != expected["size"]):
                        raise LifecycleError("Export product archive member is invalid")
                    digest = hashlib.sha256()
                    saved = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    saved.compress_type = zipfile.ZIP_DEFLATED
                    saved.external_attr = 0o100600 << 16
                    with source.open(info) as reader, output.open(saved, "w", force_zip64=True) as writer:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            digest.update(chunk)
                            writer.write(chunk)
                    if digest.hexdigest() != expected["sha256"]:
                        raise LifecycleError("Export product archive member changed")
            from .collection_exports import sanitize_public
            metadata = {"schema": "hermes-product-archive/v1", "receipt_id": receipt.id,
                        "source_bundle_sha256": receipt.bundle_sha256, "product": sanitize_public(snapshot),
                        "files": [members[name] for name in sorted(members)]}
            info = zipfile.ZipInfo("archive.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            output.writestr(info, canonical(metadata).encode())
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        if destination.exists():
            if _sha(destination) != _sha(Path(temporary)):
                raise LifecycleError("Existing archive identity conflicts with this export")
            os.unlink(temporary)
        else:
            os.replace(temporary, destination)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ProductArchive(id=archive_id, receipt_id=receipt.id, product_id=product_id,
            title=str(metadata["product"].get("product", {}).get("title") or ""),
            bundle_path=destination.relative_to(Path(store.root).resolve()).as_posix(),
            bundle_sha256=_sha(destination), size=destination.stat().st_size, metadata_json=metadata)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, LifecycleError):
            raise
        raise LifecycleError("Product archive could not be durably saved; collection data was retained") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _archive_public(record):
    product = record.metadata_json.get("product", {}).get("product", {})
    return {"id": record.id, "receipt_id": record.receipt_id, "product_id": record.product_id,
            "title": record.title, "filename": "hermes-product-" + record.product_id + ".zip",
            "size": record.size, "sha256": record.bundle_sha256, "created_at": record.created_at,
            "product_status": product.get("status"), "coverage_status": product.get("coverage_status"),
            "complete": product.get("status") == "completed" and product.get("coverage_status") == "complete",
            "download_url": "/api/v1/collection/archives/" + record.id + "/download"}


def list_archives(store):
    initialize_lifecycle(store)
    with store.session() as session:
        return [_archive_public(row) for row in session.scalars(select(ProductArchive).order_by(ProductArchive.created_at.desc(), ProductArchive.id))]


def archive_download_path(store, archive_id):
    initialize_lifecycle(store)
    with store.session() as session:
        row = session.get(ProductArchive, _id(archive_id))
        if row is None:
            raise LifecycleError("Product archive not found", 404)
        return _verified_file(store, row.bundle_path, row.bundle_sha256, row.size, "collection-archives")


def _private_tree(store, run_id, product_id):
    path = _path(store, "exports/" + _id(run_id) + "/" + _id(product_id), "exports", missing=True)
    if not path.exists():
        return path, []
    if not path.is_dir():
        raise LifecycleError("Private product export directory is invalid")
    result = []
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(Path(store.root).resolve()).as_posix()
        item = _path(store, relative)
        if item.is_file():
            result.append({"path": relative, "size": item.stat().st_size, "sha256": _sha(item)})
        elif not item.is_dir():
            raise LifecycleError("Private product export contains an unsupported file")
    return path, result


def _cleanup_private(store, receipt_id):
    """Best-effort after-commit cleanup, repeatable without touching source data."""
    with store.session(immediate=True) as session:
        receipt = session.get(ExportReceipt, receipt_id, with_for_update=not store.sqlite)
        result = dict(receipt.result)
        cleanup = dict(result["cleanup"])
        # Keep the aggregate download as recovery material until every selected
        # compact archive still verifies, including after an interrupted retry.
        for archive in session.scalars(select(ProductArchive).where(ProductArchive.receipt_id == receipt_id)):
            _verified_file(store, archive.bundle_path, archive.bundle_sha256, archive.size, "collection-archives")
        pending, removed, released = [], 0, 0
        for item in cleanup.get("pending_private_files", []):
            try:
                path = _path(store, item["path"], "exports", missing=True)
                if not path.exists():
                    continue
                if not path.is_file() or path.stat().st_size != item["size"] or _sha(path) != item["sha256"]:
                    pending.append(item)
                    continue
                path.unlink()
                removed += 1
                released += item["size"]
            except (LifecycleError, OSError):
                pending.append(item)
        cleanup.update(pending_private_files=pending,
            deleted_private_files=cleanup.get("deleted_private_files", 0) + removed,
            deleted_private_bytes=cleanup.get("deleted_private_bytes", 0) + released)
        remaining_directories = []
        for relative in cleanup.get("private_directories", []):
            try:
                directory = _path(store, relative, "exports", missing=True)
                if directory.is_dir():
                    for item in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if item.is_dir() and not item.is_symlink():
                            item.rmdir()
                    directory.rmdir()
            except (LifecycleError, OSError):
                remaining_directories.append(relative)
        cleanup["private_directories"] = remaining_directories
        artifact_pending = []
        for item in cleanup.get("pending_export_files", []):
            try:
                path = _path(store, item["path"], "collection-exports", missing=True)
                if not path.exists():
                    continue
                if not path.is_file() or path.stat().st_size != item["size"] or _sha(path) != item["sha256"]:
                    artifact_pending.append(item)
                    continue
                path.unlink()
                cleanup["deleted_export_files"] = cleanup.get("deleted_export_files", 0) + 1
                cleanup["deleted_export_bytes"] = cleanup.get("deleted_export_bytes", 0) + item["size"]
            except (LifecycleError, OSError):
                artifact_pending.append(item)
        cleanup["pending_export_files"] = artifact_pending
        cleanup["status"] = "pending_private_cleanup" if pending or remaining_directories or artifact_pending else "complete"
        result["cleanup"] = cleanup
        receipt.result = result
        return result


def finalize_export(store, receipt_id, archive_product_ids, *, saved=False):
    """Finalize exactly one acknowledged, unchanged export; repeat calls are CAS.

    Unchecked products *in this receipt* are deleted. Products outside this
    receipt, campaign controllers, shared response objects and Runs are retained.
    The caller must keep campaign/automation association edits serialized with
    finalization; this routine refuses every existing protected reference.
    """
    if saved is not True:
        raise LifecycleError("Confirm that the export was saved before archiving or deleting products", 422)
    if (not isinstance(archive_product_ids, list) or len(archive_product_ids) > 10000
            or any(type(item) is not str for item in archive_product_ids)
            or len(set(archive_product_ids)) != len(archive_product_ids)):
        raise LifecycleError("Choose distinct exported products to archive", 422)
    identifier = _id(receipt_id)
    archive_selection = sorted(_id(item) for item in archive_product_ids)
    initialize_lifecycle(store)
    with store.session(immediate=True) as session:
        receipt = session.get(ExportReceipt, identifier, with_for_update=not store.sqlite)
        if receipt is None:
            raise LifecycleError("Export receipt not found", 404)
        snapshots = _products(receipt)
        if set(archive_selection) - set(snapshots):
            raise LifecycleError("Archive selection includes a product outside this export", 422)
        if receipt.status == "finalized":
            if receipt.finalize_selection != archive_selection:
                raise LifecycleError("This export was already finalized with a different archive selection")
        else:
            if receipt.status != "ready":
                raise LifecycleError("Export is not ready for finalization")
            bundle = _verified_file(store, receipt.bundle_path, receipt.bundle_sha256, receipt.size, "collection-exports")
            from .collection_exports import product_snapshot
            products, private_files, private_directories, runs = [], [], [], set()
            for product_id, expected in snapshots.items():
                product = session.get(Product, product_id, with_for_update=not store.sqlite)
                if product is None:
                    raise LifecycleError("An exported product is missing; collection data was retained")
                if product.status not in {"paused", "completed", "completed_with_gaps", "partial", "failed", "cancelled", "needs_attention", "access_required"}:
                    raise LifecycleError("Pause active products before finalizing their export")
                run = session.get(Run, product.run_id, with_for_update=not store.sqlite)
                if run is None or run.status not in {"paused", "completed", "completed_with_gaps", "failed", "cancelled", "needs_attention", "access_required"}:
                    raise LifecycleError("Pause the harvest before cleanup so its catalog cannot be rewritten concurrently")
                current = product_snapshot(store, product_id, session=session)
                if current.get("fingerprint") != expected.get("fingerprint"):
                    raise LifecycleError("Product changed after export; export the current snapshot before cleanup")
                _protected(store, session, product)
                directory, files = _private_tree(store, product.run_id, product_id)
                private_files.extend(files)
                private_directories.append(directory.relative_to(Path(store.root).resolve()).as_posix())
                products.append(product)
                runs.add(product.run_id)
            # Combined run catalogs contain selected products too. They are
            # derived caches, so invalidate them without touching the private
            # exports or source rows of unselected products in the same Run.
            for run_id in sorted(runs):
                directory, files = _private_tree(store, run_id, "catalog")
                private_files.extend(files)
                private_directories.append(directory.relative_to(Path(store.root).resolve()).as_posix())
            archives = []
            for product_id in archive_selection:
                try:
                    record = _stage_archive(store, receipt, snapshots[product_id], bundle)
                except OSError:
                    raise LifecycleError("Product archive storage failed; collection data was retained") from None
                session.add(record)
                session.flush()
                archives.append(_archive_public(record))
            # Compression can take time. Recheck private artifact/controller
            # changes immediately before mutation, still under the DB locks.
            for product in products:
                if product_snapshot(store, product.id, session=session).get("fingerprint") != snapshots[product.id]["fingerprint"]:
                    raise LifecycleError("Product changed while archiving; collection data was retained")
                _protected(store, session, product)
            # All archives are on disk and hash-indexed before the first delete.
            for product in products:
                for model in (Batch, BrowserBranch, ProductRecordScope, TrafficWindow, ResponseObservation):
                    session.execute(delete(model).where(model.product_id == product.id))
                session.execute(delete(Event).where(Event.run_id == product.run_id, Event.payload["product_id"].as_string() == product.id))
                session.delete(product)
            result = {"receipt_id": identifier, "status": "finalized", "archived": archives,
                      "archived_product_ids": archive_selection,
                      "deleted_product_ids": sorted(set(snapshots) - set(archive_selection)),
                      "removed_from_collection_product_ids": sorted(snapshots),
                      "cleanup": {"status": "pending_private_cleanup", "deleted_private_files": 0,
                                  "deleted_private_bytes": 0, "pending_private_files": private_files,
                                  "private_directories": private_directories,
                                  "invalidated_catalog_run_ids": sorted(runs),
                                  "deleted_export_files": 0, "deleted_export_bytes": 0,
                                  "pending_export_files": [{"path": receipt.bundle_path,
                                      "sha256": receipt.bundle_sha256, "size": receipt.size}],
                                  "shared_cache": "retained_unproven_or_shared_references",
                                  "runs_and_campaigns": "retained"}}
            # The sidecar is removable only when it is exactly this receipt's
            # metadata. An unexpected sidecar stays available for inspection.
            sidecar = _path(store, str(PurePosixPath(receipt.bundle_path).parent / "manifest.json"),
                            "collection-exports", missing=True)
            if sidecar.is_file():
                try:
                    if json.loads(sidecar.read_text()) == receipt.manifest:
                        result["cleanup"]["pending_export_files"].append({
                            "path": sidecar.relative_to(Path(store.root).resolve()).as_posix(),
                            "sha256": _sha(sidecar), "size": sidecar.stat().st_size})
                except (OSError, ValueError):
                    pass
            receipt.status, receipt.finalized_at = "finalized", time.time()
            receipt.finalize_selection, receipt.result = archive_selection, result
    return _cleanup_private(store, identifier)
