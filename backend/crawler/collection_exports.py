"""Selective local-only collection bundles with exact durable snapshot boundaries.

Exports never use transport or Store.get_object (which may fetch from S3).
Every saved row is retained, including legacy/partial observations, after removing
credential material. A receipt is evidence of an export, not of product completion.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from .exporter import export_rows
from .store import Batch, BrowserBranch, Event, Product, ProductRecordScope, Response, ResponseObservation, Run, TrafficWindow

_ID = re.compile(r"[a-f0-9]{32}\Z")
_OBJECT = re.compile(r"objects/([a-f0-9]{2})/([a-f0-9]{64})\.jsonl\.gz\Z")
_IMAGE = re.compile(r"([a-f0-9]{64})\.(png|jpg|jpeg|gif|webp|bin)\Z")
_PRIVATE = re.compile(r"secret|token|cookie|password|authorization|credential|headers|session_state|storage_state|api_?key|private|access_?key|profile_path|browser_profile|user_data_dir|local_path|(?:^|_)(?:raw|post_data|request_body|response_body|page_source)(?:_|$)", re.I)
_PRIVATE_NAMES = {"form_key", "formkey", "csrf", "csrf_token", "csrftoken", "xsrf", "xsrf_token", "xsrftoken",
                  "nonce", "session", "session_id", "sessionid", "phpsessid", "sid", "auth", "jwt", "sig", "signature"}
_MAX_ROW = 16 * 1024 * 1024
_MAX_METADATA = 32 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_PRODUCTS = 1_000


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid collection identifier")
    return value


def _key(value):
    return re.sub(r"([a-z])([A-Z])", r"\1_\2", str(value)).lower().replace("-", "_")


def _private_key(value):
    normalized = _key(value)
    return bool(_PRIVATE.search(normalized) or normalized in _PRIVATE_NAMES
                or normalized in {"x_amz_signature", "x_amz_credential", "x_amz_security_token"})


def sanitize_public(value, depth=0):
    """Keep saved pricing/option data; never include account/session objects."""
    if depth > 40:
        return "[nested value omitted]"
    if isinstance(value, dict):
        return {str(k): sanitize_public(v, depth + 1) for k, v in value.items() if not _private_key(k)}
    if isinstance(value, (list, tuple)):
        return [sanitize_public(v, depth + 1) for v in value]
    if isinstance(value, str):
        if value.startswith(("/", "file://", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value):
            return "[local path omitted]"
        if value.startswith(("http://", "https://")):
            try:
                u = urlsplit(value)
                host = u.hostname or ""
                if u.port:
                    host += ":" + str(u.port)
                query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                         if not _private_key(k) and _key(k) not in {"cid", "itemid", "item_id", "cartid", "cart_id"}]
                return urlunsplit((u.scheme, host, u.path, urlencode(query), ""))
            except ValueError:
                return "[invalid source URL omitted]"
        # CSV cells can contain structured audit JSON from older exports.
        if value[:1] in {"{", "["} and len(value) <= _MAX_ROW:
            try:
                return _json(sanitize_public(json.loads(value), depth + 1))
            except (ValueError, TypeError):
                pass
        return value
    return value


def _local(root, relative, *, exists=True):
    root = Path(root).absolute()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid collection file path")
    path = root / relative
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Collection symlink rejected")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Collection file outside data directory")
    if exists and not path.is_file():
        raise FileNotFoundError("Saved collection file is missing")
    return path


def _file_info(path):
    before = path.stat()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Invalid saved collection file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Saved collection file changed during export")
    return {"sha256": digest.hexdigest(), "size": after.st_size}


def _export_tree(store, run_id, product_id):
    relative = f"exports/{run_id}/{product_id}"
    path = _local(store.root, relative, exists=False)
    if not path.exists():
        return {"root": relative, "exists": False, "files": []}
    if not path.is_dir():
        raise ValueError("Invalid product export directory")
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError("Collection symlink rejected")
        if item.is_file():
            if len(files) >= _MAX_FILES:
                raise ValueError("Product file inventory exceeds export bound")
            files.append({"path": str(item.relative_to(path)), **_file_info(item)})
    return {"root": relative, "exists": True, "files": files}


def _record(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def product_snapshot(store, product_id, *, session=None):
    """Exact CAS identity; optional caller transaction supports stopped cleanup."""
    _identifier(product_id)
    if session is None:
        with store.session(immediate=True) as own:
            return product_snapshot(store, product_id, session=own)
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError("Collection product not found")
    run = session.get(Run, product.run_id)
    if run is None:
        raise ValueError("Collection run is missing")
    batches = list(session.scalars(select(Batch).where(Batch.product_id == product_id).order_by(Batch.sequence, Batch.id)))
    if sum(b.row_count for b in batches) != product.row_count:
        raise ValueError("Saved product row boundary is inconsistent")
    branches = list(session.scalars(select(BrowserBranch).where(BrowserBranch.product_id == product_id).order_by(BrowserBranch.id)))
    observations = list(session.scalars(select(ResponseObservation).where(ResponseObservation.product_id == product_id).order_by(ResponseObservation.id)))
    scope = session.get(ProductRecordScope, product_id)
    traffic = session.get(TrafficWindow, product_id)
    events = list(session.scalars(select(Event).where(Event.run_id == run.id,
        Event.payload["product_id"].as_string() == product_id).order_by(Event.id)))
    snapshot = {"schema": "collection-product-snapshot/v1", "product_id": product_id, "run_id": run.id,
        "product": {"url": product.url, "title": product.title, "status": product.status,
                    "coverage_status": product.coverage_status, "row_count": product.row_count,
                    "epoch": product.epoch, "owner": product.owner, "export_manifest_sha256": _hash(product.export_manifest)},
        "product_record_sha256": _hash(_record(product)),
        "run": {"id": run.id, "name": run.name, "status": run.status, "spec_sha256": _hash(run.spec)},
        "batches": [{"id": b.id, "sequence": b.sequence, "row_count": b.row_count,
                     "object_path": b.object_path, "keys_sha256": _hash(b.keys)} for b in batches],
        "branches": [{"id": b.id, "scope": b.scope, "prefix_sha256": _hash(b.prefix),
                      "rows_object": b.rows_object, "row_count": b.row_count,
                      "committed_row_count": b.committed_row_count, "details_sha256": _hash(b.details),
                      "completed_at": b.completed_at} for b in branches],
        "response_observations": [{"id": o.id, "response_id": o.response_id, "kind": o.kind,
            "request_signature": o.request_signature, "created_at": o.created_at, "context_sha256": _hash(o.context),
            "response_identity_sha256": _hash(_record(response)) if (response := session.get(Response, o.response_id)) else None}
            for o in observations],
        "events": [{"id": e.id, "type": e.type, "created_at": e.created_at, "payload_sha256": _hash(e.payload)} for e in events],
        "traffic_window": _record(traffic) if traffic else None,
        "record_scope": _record(scope) if scope else None,
        "export_tree": _export_tree(store, run.id, product_id),
        "boundary": {"batch_ids": [b.id for b in batches], "row_count": product.row_count}}
    snapshot["fingerprint"] = _hash(snapshot)
    return snapshot


def _summary(snapshot):
    p, r = snapshot["product"], snapshot["run"]
    complete = p["status"] == "completed" and p["coverage_status"] == "complete"
    scope = snapshot.get("record_scope")
    legacy = scope.get("legacy_row_count", 0) if scope else None
    return {"id": snapshot["product_id"], "product_id": snapshot["product_id"], "run_id": snapshot["run_id"],
        "run_name": sanitize_public(r["name"]), "title": sanitize_public(p["title"]), "url": sanitize_public(p["url"]),
        "status": p["status"], "coverage_status": p["coverage_status"], "row_count": p["row_count"],
        "complete": complete, "requires_incomplete_ack": not complete,
        "active": p["status"] in {"running", "queued", "pending", "discovering", "extracting"},
        "archived": False, "exists": True, "exportable": True, "legacy_row_count": legacy,
        "record_scope": "service_context_v2" if scope else "not_assessed",
        "scoped_row_count": max(0, p["row_count"] - legacy) if legacy is not None else None,
        "snapshot_fingerprint": snapshot["fingerprint"]}


def list_collection_products(store):
    """Global saved products; counts alone never imply complete coverage."""
    with store.session() as session:
        rows = list(session.execute(select(Product, Run).join(Run, Run.id == Product.run_id).order_by(Run.created_at.desc(), Product.id)))
        timings = store.product_timings(session, [p for p, _ in rows], {r.id: r for _, r in rows})
        ids, scopes = [p.id for p, _ in rows], {}
        for offset in range(0, len(ids), 500):
            scopes.update({scope.product_id: scope for scope in session.scalars(select(ProductRecordScope).where(
                ProductRecordScope.product_id.in_(ids[offset:offset + 500])))})
        items = []
        for p, r in rows:
            scope = scopes.get(p.id)
            snap = {"product_id": p.id, "run_id": r.id,
                    "product": _record(p), "run": _record(r), "record_scope": _record(scope) if scope else None,
                    "fingerprint": None}
            items.append({**_summary(snap), "timing": timings.get(p.id)})
    return {"items": items}


def _rows(store, snapshot, image_urls):
    count = 0
    for batch in snapshot["batches"]:
        match = _OBJECT.fullmatch(batch["object_path"] or "")
        if not match or match[1] != match[2][:2]:
            raise ValueError("Invalid saved row object")
        path = _local(store.root, batch["object_path"])
        if _file_info(path)["sha256"] != match[2]:
            raise ValueError("Saved row object hash changed")
        batch_count = 0
        keys = []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            while line := handle.readline(_MAX_ROW + 1):
                if len(line.encode()) > _MAX_ROW:
                    raise ValueError("Saved row exceeds export bound")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Invalid saved row")
                row = sanitize_public(row)
                keys.append(row.get("id"))
                for item in row.get("images", []) if isinstance(row.get("images"), list) else []:
                    url = item if isinstance(item, str) else item.get("url", item.get("source_url")) if isinstance(item, dict) else None
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        image_urls.add(url)
                count += 1
                batch_count += 1
                yield row
        if batch_count != batch["row_count"] or _hash(keys) != batch["keys_sha256"]:
            raise ValueError("Saved row batch count changed")
    if count != snapshot["boundary"]["row_count"]:
        raise ValueError("Saved row snapshot count changed")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def _safe_csv(source, target):
    """Escape spreadsheet formulas; JSONL/Parquet retain the sanitized values."""
    target.parent.mkdir(parents=True, exist_ok=True)
    csv.field_size_limit(max(csv.field_size_limit(), _MAX_ROW))
    with source.open(newline="", encoding="utf-8") as incoming, target.open("w", newline="", encoding="utf-8") as outgoing:
        reader, writer = csv.reader(incoming), csv.writer(outgoing, lineterminator="\n")
        keep = None
        for row in reader:
            if keep is None:
                keep = [index for index, name in enumerate(row) if not _private_key(name)]
            cells = []
            for index, cell in enumerate(row):
                if index not in keep:
                    continue
                cell = sanitize_public(cell)
                cells.append("'" + cell if cell.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"} else cell)
            writer.writerow(cells)


def _copy_images(store, snapshot, target, image_urls):
    root = _local(store.root, snapshot["export_tree"]["root"], exists=False)
    directory = root / "images"
    source_manifest = directory / "images.json"
    inventory = {entry["path"]: entry for entry in snapshot["export_tree"]["files"]}
    declared, notes = {}, []
    if "images/images.json" in inventory:
        _local(store.root, source_manifest.relative_to(store.root))
        if _file_info(source_manifest) != {k: inventory["images/images.json"][k] for k in ("sha256", "size")}:
            raise ValueError("Saved image manifest changed during export")
        if source_manifest.stat().st_size > _MAX_METADATA:
            raise ValueError("Saved image manifest exceeds export bound")
        data = json.loads(source_manifest.read_text())
        if not isinstance(data, list):
            raise ValueError("Invalid saved image manifest")
        for entry in data:
            if isinstance(entry, dict):
                url = sanitize_public(entry.get("url", ""))
                if url:
                    image_urls.add(url)
                if isinstance(entry.get("file"), str):
                    declared.setdefault(entry["file"], []).append((url, entry.get("status")))
                elif url:
                    notes.append({"source_url": url, "status": "unavailable", "reason": "saved_download_failed"})
    included, covered = set(), set()
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Invalid saved image directory")
        image_paths = [name for name in inventory if Path(name).parent == Path("images") and name != "images/images.json"]
        for name in sorted(image_paths):
            path = _local(store.root, Path(snapshot["export_tree"]["root"]) / name)
            if path.is_symlink():
                raise ValueError("Collection symlink rejected")
            if not path.is_file() or path.name == "images.json":
                continue
            match = _IMAGE.fullmatch(path.name)
            if not match or match[2] == "bin" and not any(status == "downloaded" for _, status in declared.get(path.name, [])):
                notes.append({"file": path.name, "status": "excluded", "reason": "unrecognized_saved_image"})
                continue
            info = _file_info(path)
            if info != {k: inventory[name][k] for k in ("sha256", "size")} or info["sha256"] != match[1]:
                raise ValueError("Saved image hash changed")
            destination = target / "images" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            urls = [url for url, status in declared.get(path.name, []) if url and status == "downloaded"]
            covered.update(urls)
            included.add(path.name)
            notes.append({"file": path.name, "path": "images/" + path.name, "source_urls": urls, "status": "included", **info})
    for filename, entries in declared.items():
        if filename not in included:
            for url, status in entries:
                if url and status == "downloaded":
                    covered.add(url)
                    notes.append({"source_url": url, "status": "missing_file", "reason": "saved_image_file_missing"})
    already = covered | {n.get("source_url") for n in notes}
    notes.extend({"source_url": url, "status": "not_downloaded", "reason": "no_saved_download"} for url in sorted(image_urls - already))
    _write_json(target / "images.json", {"source_fetches": 0, "items": notes})
    return notes


def _saved_metadata(store, snapshot, target):
    root = _local(store.root, snapshot["export_tree"]["root"], exists=False)
    excluded = []
    for entry in snapshot["export_tree"]["files"]:
        relative = Path(entry["path"])
        if relative.parts[0] == "images":
            continue
        path = _local(store.root, root.relative_to(store.root) / relative)
        if _file_info(path) != {k: entry[k] for k in ("sha256", "size")}:
            raise ValueError("Saved product artifact changed during export")
        destination = target / "saved-artifacts" / relative
        if _private_key(relative.name) or _private_key(relative.stem) or relative.suffix.lower() not in {".json", ".jsonl", ".csv"}:
            excluded.append({"path": str(relative), "reason": "unsafe_or_rebuilt_format"})
            continue
        if entry["size"] > _MAX_METADATA:
            excluded.append({"path": str(relative), "reason": "metadata_size_bound"})
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if relative.suffix == ".csv":
                _safe_csv(path, destination)
            elif relative.suffix == ".json":
                _write_json(destination, sanitize_public(json.loads(path.read_text())))
            else:
                with path.open(encoding="utf-8") as incoming, destination.open("w", encoding="utf-8") as outgoing:
                    for line in incoming:
                        outgoing.write(_json(sanitize_public(json.loads(line))) + "\n")
        except (ValueError, UnicodeError, csv.Error):
            destination.unlink(missing_ok=True)
            excluded.append({"path": str(relative), "reason": "unrecognized_metadata_format"})
    return excluded


def _public_product(snapshot):
    result = _summary(snapshot)
    result.update({key: snapshot[key] for key in ("archive_prefix", "warnings", "incomplete_acknowledged") if key in snapshot})
    result["exported_row_count"] = snapshot["boundary"]["row_count"]
    return result


def _public_receipt(receipt):
    result = {"id": receipt.id, "filename": receipt.manifest["bundle_filename"],
        "url": f"/api/v1/collection/exports/{receipt.id}/download", "size": receipt.size,
        "bundle_sha256": receipt.bundle_sha256, "created_at": receipt.created_at, "status": receipt.status,
        "products": [_public_product(p) for p in receipt.manifest["products"]]}
    cleanup = (receipt.result or {}).get("cleanup")
    if isinstance(cleanup, dict):
        safe = {"status": cleanup.get("status") if cleanup.get("status") in {"pending_private_cleanup", "complete"} else "unknown"}
        for key in ("deleted_private_files", "deleted_private_bytes", "deleted_export_files", "deleted_export_bytes"):
            value = cleanup.get(key, 0)
            safe[key] = value if type(value) is int and value >= 0 else 0
        for public, private in (("pending_private_file_count", "pending_private_files"),
                                ("pending_export_file_count", "pending_export_files"),
                                ("pending_directory_count", "private_directories")):
            safe[public] = len(cleanup[private]) if isinstance(cleanup.get(private), list) else 0
        result["cleanup"] = safe
    if receipt.status == "finalized":
        selection = receipt.finalize_selection
        result["finalize_selection"] = selection if isinstance(selection, list) and all(isinstance(i, str) and _ID.fullmatch(i) for i in selection) else []
    return result


def _pending_overlap(store, session, snapshots):
    from .collection_lifecycle import ExportReceipt
    expected = {p["product_id"]: p["fingerprint"] for p in snapshots}
    for receipt in session.scalars(select(ExportReceipt).where(ExportReceipt.status == "ready")):
        existing = {p["product_id"]: p["fingerprint"] for p in receipt.manifest["products"]}
        if not expected.keys() & existing.keys():
            continue
        if existing != expected:
            continue
        path = _local(store.root, f"collection-exports/{receipt.id}/collection.zip")
        if receipt.bundle_path != str(path.relative_to(store.root)) or _file_info(path) != {"sha256": receipt.bundle_sha256, "size": receipt.size}:
            raise ValueError("Existing collection export file changed")
        return receipt
    return None


def create_export_receipt(store, request):
    from .collection_lifecycle import ExportReceipt, initialize_lifecycle
    if not isinstance(request, dict) or set(request) != {"product_ids", "allow_incomplete_product_ids"}:
        raise ValueError("Exact collection export selection required")
    ids, allowed = request["product_ids"], request["allow_incomplete_product_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= _MAX_PRODUCTS
            or not isinstance(allowed, list) or len(allowed) > _MAX_PRODUCTS):
        raise ValueError("Invalid collection export selection")
    for item in [*ids, *allowed]:
        _identifier(item)
    if len(set(ids)) != len(ids) or len(set(allowed)) != len(allowed):
        raise ValueError("Duplicate collection export selection")
    if not set(allowed) <= set(ids):
        raise ValueError("Incomplete acknowledgement outside selection")
    initialize_lifecycle(store)
    with store.session(immediate=True) as session:
        snapshots = [product_snapshot(store, pid, session=session) for pid in ids]
    incomplete = {p["product_id"] for p in snapshots if not _summary(p)["complete"]}
    if set(allowed) != incomplete:
        raise ValueError("Explicit acknowledgement required for each incomplete product")
    with store.session(immediate=True) as session:
        existing = _pending_overlap(store, session, snapshots)
        if existing is not None:
            return _public_receipt(existing)
    identifier = uuid.uuid4().hex
    parent = _local(store.root, "collection-exports", exists=False)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=parent))
    final = parent / identifier
    try:
        content = staging / "content"
        content.mkdir()
        for snapshot in snapshots:
            pid = snapshot["product_id"]
            title = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(snapshot["product"]["title"] or "")).strip("-")[:60] or "product"
            prefix = f"products/{title}-{pid}"
            target = content / prefix
            target.mkdir(parents=True)
            snapshot["archive_prefix"] = prefix + "/"
            snapshot["incomplete_acknowledged"] = pid in incomplete
            warnings = ["All saved rows are included; per-row evidence does not establish complete product coverage."]
            if pid in incomplete:
                warnings.append("INCOMPLETE PRODUCT: exported by explicit acknowledgement; unexplored or unsupported options may be missing.")
            if (snapshot.get("record_scope") or {}).get("legacy_row_count", 0):
                warnings.append("Legacy rows are included and may lack current service-scoped evidence.")
            elif snapshot.get("record_scope") is None and snapshot["boundary"]["row_count"]:
                warnings.append("Service scope has not been classified; saved rows are retained without implying current verification.")
            snapshot["warnings"] = warnings
            urls = set()
            jsonl = target / "rows.jsonl"
            with jsonl.open("w", encoding="utf-8") as output:
                def rows():
                    for row in _rows(store, snapshot, urls):
                        output.write(_json(row) + "\n")
                        yield row
                generated = export_rows(rows(), staging / ("dataset-" + pid), metadata={
                    "source_row_count": snapshot["boundary"]["row_count"], "legacy_row_count": (snapshot.get("record_scope") or {}).get("legacy_row_count", 0)})
            if generated["row_count"] != snapshot["boundary"]["row_count"]:
                raise ValueError("Exported snapshot row count changed")
            for item in generated["items"]:
                if item["name"] == "combined.csv":
                    _safe_csv(Path(item["path"]), target / "rows.csv")
                elif item["name"] == "combined.parquet":
                    shutil.copyfile(item["path"], target / "rows.parquet")
            images = _copy_images(store, snapshot, target, urls)
            excluded = _saved_metadata(store, snapshot, target)
            _write_json(target / "metadata.json", {**_public_product(snapshot), "scope": "all_saved_rows_at_batch_boundary",
                "batch_ids": snapshot["boundary"]["batch_ids"], "images_included": sum(x["status"] == "included" for x in images),
                "excluded_saved_artifacts": excluded, "source_fetches": 0,
                "csv_formula_escaping": "Potential spreadsheet formulas are apostrophe-prefixed; sanitized JSONL and Parquet preserve values."})
        files = [{"path": str(p.relative_to(content)), **_file_info(p)} for p in sorted(content.rglob("*")) if p.is_file()]
        filename = f"hermes-collection-{identifier[:12]}.zip"
        _write_json(content / "manifest.json", {"schema": "collection-bundle/v1", "products": [_public_product(p) for p in snapshots],
            "files": files, "source_fetches": 0, "scope": "all_saved_rows_at_recorded_batch_boundaries"})
        bundle = staging / "collection.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in sorted(content.rglob("*")):
                if path.is_file():
                    archive.write(path, str(path.relative_to(content)))
        with bundle.open("rb") as handle:
            os.fsync(handle.fileno())
        info = _file_info(bundle)
        manifest = {"schema": "collection-export-receipt/v1", "products": snapshots, "files": files, "bundle_filename": filename}
        _write_json(staging / "manifest.json", manifest)
        shutil.rmtree(content)
        for path in staging.glob("dataset-*"):
            shutil.rmtree(path)
        os.replace(staging, final)
        reused = False
        with store.session(immediate=True) as session:
            if any(session.get(Product, p["product_id"]) is None for p in snapshots):
                raise ValueError("Collection product was archived during export")
            receipt = _pending_overlap(store, session, snapshots)
            if receipt is None:
                receipt = ExportReceipt(id=identifier, created_at=time.time(), status="ready",
                    bundle_path=f"collection-exports/{identifier}/collection.zip", bundle_sha256=info["sha256"], size=info["size"], manifest=manifest)
                session.add(receipt)
                session.flush()
            else:
                reused = True
            result = _public_receipt(receipt)
        if reused:
            shutil.rmtree(final)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        raise


def read_export_receipt(store, receipt_id):
    from .collection_lifecycle import ExportReceipt, initialize_lifecycle
    _identifier(receipt_id)
    initialize_lifecycle(store)
    with store.session() as session:
        receipt = session.get(ExportReceipt, receipt_id)
        if receipt is None:
            raise LookupError("Collection export not found")
        return _public_receipt(receipt)


def list_pending_exports(store):
    from .collection_lifecycle import ExportReceipt, initialize_lifecycle
    initialize_lifecycle(store)
    with store.session() as session:
        receipts = session.scalars(select(ExportReceipt).where(ExportReceipt.status.in_(["ready", "finalized"])).order_by(ExportReceipt.created_at.desc(), ExportReceipt.id))
        return {"items": [_public_receipt(receipt) for receipt in receipts
            if receipt.status == "ready" or isinstance(receipt.result, dict)
            and isinstance(receipt.result.get("cleanup"), dict)
            and receipt.result["cleanup"].get("status") == "pending_private_cleanup"]}


def discard_export(store, receipt_id):
    """Explicitly discard a pending delivery artifact; source rows are untouched."""
    from .collection_lifecycle import ExportReceipt, initialize_lifecycle
    _identifier(receipt_id)
    initialize_lifecycle(store)
    with store.session(immediate=True) as session:
        receipt = session.get(ExportReceipt, receipt_id, with_for_update=not store.sqlite)
        if receipt is None:
            raise LookupError("Collection export not found")
        if receipt.status == "discarded":
            return {"id": receipt_id, "status": "discarded", "products_unchanged": True}
        if receipt.status != "ready":
            raise ValueError("Only pending collection exports can be discarded")
        expected = f"collection-exports/{receipt_id}/collection.zip"
        if receipt.bundle_path != expected:
            raise ValueError("Collection export file identity changed")
        directory = _local(store.root, f"collection-exports/{receipt_id}", exists=False)
        if directory.exists():
            if not directory.is_dir() or any(item.name not in {"collection.zip", "manifest.json"} or item.is_symlink() or not item.is_file() for item in directory.iterdir()):
                raise ValueError("Unexpected collection export artifact; nothing discarded")
            for name in ("collection.zip", "manifest.json"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()
        receipt.status = "discarded"
        receipt.result = {"id": receipt_id, "status": "discarded", "products_unchanged": True}
        return dict(receipt.result)


def export_download_path(store, receipt_id):
    from .collection_lifecycle import ExportReceipt, initialize_lifecycle
    _identifier(receipt_id)
    initialize_lifecycle(store)
    with store.session() as session:
        receipt = session.get(ExportReceipt, receipt_id)
        if receipt is None:
            raise LookupError("Collection export not found")
        expected = f"collection-exports/{receipt_id}/collection.zip"
        if receipt.bundle_path != expected:
            raise ValueError("Collection export file identity changed")
        path = _local(store.root, expected)
        if _file_info(path) != {"sha256": receipt.bundle_sha256, "size": receipt.size}:
            raise ValueError("Collection export file changed")
        return path
