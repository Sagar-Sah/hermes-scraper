"""Streaming, evidence-preserving CSV/Parquet publication with atomic manifests."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .commerce import configuration, decimal_value
from .presentation import present_row
from .customer_csv import customer_columns, customer_row, format_schemas

BASE_COLUMNS = (
    "product_url",
    "source_product_id",
    "product_id",
    "title",
    "quantity",
    "currency",
    "subtotal",
    "unit_price",
    "verification_status",
    "record_schema_version",
    "record_scope",
    "configuration_json",
    "pricing_context_json",
    "provenance_json",
    "evidence_ids_json",
    "production_json",
    "record_json",
)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _product_key(row: Mapping[str, Any]) -> str:
    identity = (
        row.get("product_url") or row.get("source_product_id") or row.get("product_id")
    )
    if identity is None:
        raise ValueError(
            "Each exported row needs product_url, source_product_id, or product_id"
        )
    return str(identity)


def _slug(identity: str, title: Any) -> str:
    readable = (
        re.sub(r"[^a-zA-Z0-9_-]+", "-", str(title or identity)).strip("-")[:60]
        or "product"
    )
    return f"{readable}-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _spool_name(identity: str) -> str:
    # Internal scratch file; kept short to stay under Windows' 260-char MAX_PATH.
    return f"{hashlib.sha256(identity.encode()).hexdigest()[:16]}.jsonl"


def _normalize(row: Mapping[str, Any]) -> dict[str, Any]:
    row = present_row(row)
    config = configuration(row)
    result = {
        key: None if row.get(key) is None else str(row[key])
        for key in BASE_COLUMNS
        if not key.endswith("_json")
    }
    for key in ("subtotal", "unit_price"):
        if row.get(key) is not None:
            result[key] = format(decimal_value(row[key]), "f")
    result["verification_status"] = str(row.get("verification_status") or "provisional")
    result.update(
        {
            "configuration_json": _json(config),
            "pricing_context_json": _json(row.get("pricing_context", {})),
            "provenance_json": _json(row.get("provenance", {})),
            "evidence_ids_json": _json(row.get("evidence_ids", [])),
            "production_json": _json(row.get("production")),
            "record_json": _json(dict(row)),
        }
    )
    for key, value in config.items():
        result[f"attr:{key}"] = (
            _json(value)
            if isinstance(value, (dict, list, tuple, bool))
            else None if value is None else str(value)
        )
    return result


def _sync(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some platforms/filesystems do not permit directory fsync. File fsync
        # and atomic rename still preserve visibility, but not power-loss claims.
        pass


def _write_dataset(
    spool: Path, target: Path, columns: list[str], csv_columns: list[str], batch_size: int
) -> tuple[Path, Path, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    target.parent.mkdir(parents=True, exist_ok=True)
    csv_path, parquet_path = target.with_suffix(".csv"), target.with_suffix(".parquet")
    schema = pa.schema([pa.field(name, pa.string()) for name in columns])
    total = 0
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file, pq.ParquetWriter(
        parquet_path, schema, compression="zstd"
    ) as parquet_writer:
        writer = csv.DictWriter(
            csv_file, fieldnames=csv_columns, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        batch = []
        with spool.open("r", encoding="utf-8") as source:
            for line in source:
                source_row = json.loads(line)
                row = _normalize(source_row)
                writer.writerow(customer_row(source_row))
                batch.append(row)
                total += 1
                if len(batch) >= batch_size:
                    parquet_writer.write_table(
                        pa.Table.from_pylist(batch, schema=schema),
                        row_group_size=batch_size,
                    )
                    batch.clear()
        if batch:
            parquet_writer.write_table(
                pa.Table.from_pylist(batch, schema=schema), row_group_size=batch_size
            )
        csv_file.flush()
        os.fsync(csv_file.fileno())
    _sync(parquet_path)
    return csv_path, parquet_path, total


def export_rows(
    rows: Iterable[Mapping[str, Any]],
    output_dir: str | Path,
    *,
    batch_size: int = 4096,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume rows once and atomically publish a complete immutable generation.

    Memory is bounded by a batch plus product/attribute metadata. Dynamic columns
    are discovered using temporary on-disk spools, never by materializing rows.
    Prices are decimal strings. Customer CSVs contain readable labels, while
    audit Parquet retains typed configuration, IDs, evidence and full records.
    """
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < 1
    ):
        raise ValueError("batch_size must be a positive integer")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    generation = f"snapshot-{uuid.uuid4().hex}"
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=destination))
    committed = destination / generation
    manifest_temp: Path | None = None
    handles: OrderedDict[str, Any] = OrderedDict()
    try:
        spool_dir = staging / ".spool"
        spool_dir.mkdir()
        combined_spool = spool_dir / "combined.jsonl"
        products: dict[str, dict[str, Any]] = {}
        attributes: set[str] = set()
        csv_attributes: set[str] = set()
        statuses: Counter[str] = Counter()
        count = 0
        with combined_spool.open("w", encoding="utf-8") as combined:
            for incoming in rows:
                row = dict(incoming)
                identity = _product_key(row)
                config = configuration(row)
                attributes.update(f"attr:{key}" for key in config)
                csv_attributes.update(customer_row(row))
                status = str(row.get("verification_status") or "provisional")
                statuses[status] += 1
                if identity not in products:
                    products[identity] = {
                        "key": identity,
                        "slug": _slug(identity, row.get("title")),
                        "title": row.get("title"),
                        "product_url": row.get("product_url"),
                        "row_count": 0,
                        "statuses": Counter(),
                    }
                product = products[identity]
                product["row_count"] += 1
                product["statuses"][status] += 1
                encoded = _json(row) + "\n"
                combined.write(encoded)
                if identity not in handles:
                    if len(handles) >= 32:
                        _, evicted = handles.popitem(last=False)
                        evicted.close()
                    handles[identity] = (spool_dir / _spool_name(identity)).open(
                        "a", encoding="utf-8"
                    )
                handles.move_to_end(identity)
                handles[identity].write(encoded)
                count += 1
        for handle in handles.values():
            handle.close()
        handles.clear()
        columns = list(BASE_COLUMNS) + sorted(attributes)
        csv_columns = customer_columns(csv_attributes)
        items: list[dict[str, Any]] = []

        def record_files(paths: Iterable[Path], product_key: str | None = None) -> None:
            for path in paths:
                relative = path.relative_to(staging)
                items.append(
                    {
                        "name": str(relative),
                        "path": str(committed / relative),
                        "size": path.stat().st_size,
                        "format": path.suffix.lstrip("."),
                        "schema": "customer_csv" if path.suffix == ".csv" else "audit_parquet",
                        "product_key": product_key,
                    }
                )

        csv_path, parquet_path, combined_count = _write_dataset(
            combined_spool, staging / "combined", columns, csv_columns, batch_size
        )
        if combined_count != count:
            raise RuntimeError("Combined row count changed during export")
        record_files((csv_path, parquet_path))
        for identity, product in products.items():
            if len(products) == 1:
                # A single-product export already is its combined dataset.
                # Reuse that pair instead of serializing every record twice.
                for item in items:
                    item["product_key"] = identity
                product["statuses"] = dict(product["statuses"])
                continue
            csv_path, parquet_path, product_count = _write_dataset(
                spool_dir / _spool_name(identity),
                staging / "products" / product["slug"],
                columns,
                csv_columns,
                batch_size,
            )
            if product_count != product["row_count"]:
                raise RuntimeError("Product row count changed during export")
            record_files((csv_path, parquet_path), identity)
            product["statuses"] = dict(product["statuses"])
        manifest = {
            "schema_version": "2.0",
            "generation": generation,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "row_count": count,
            "product_count": len(products),
            "statuses": dict(statuses),
            "columns": csv_columns,
            "schemas": format_schemas(csv_columns, columns),
            "products": list(products.values()),
            "items": items,
            "publication_status": "published",
            "verification_claim": "Per-row evidence only; publication does not imply complete discovery or verification",
        }
        # Source totals include preserved legacy observations; row_count is the
        # actual current dataset. Only explicitly supported metadata can enter.
        for key in (
            "source_row_count",
            "legacy_row_count",
            "scoped_row_count",
            "record_scope",
        ):
            if metadata is not None and key in metadata:
                manifest[key] = metadata[key]
        shutil.rmtree(spool_dir)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _sync(staging / "manifest.json")
        _sync_directory(staging)
        os.replace(staging, committed)
        _sync_directory(destination)
        # Commit the public pointer last. Readers always see either the complete
        # previous generation or the complete replacement, never half an export.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".manifest-", suffix=".json", dir=destination
        )
        manifest_temp = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(manifest_temp, destination / "manifest.json")
        manifest_temp = None
        _sync_directory(destination)
        return manifest
    except BaseException:
        for handle in handles.values():
            handle.close()
        shutil.rmtree(staging, ignore_errors=True)
        if manifest_temp is not None:
            manifest_temp.unlink(missing_ok=True)
        raise


def query_price_analytics(parquet_path: str | Path) -> list[dict[str, Any]]:
    """Optional local DuckDB summary; never mixes currencies or configurations.

    Casts use decimal arithmetic. Quantity-break analysis with quantity-encoded
    configurations belongs in commerce.analyze_price_breaks instead.
    """
    import duckdb

    with duckdb.connect(":memory:") as connection:
        columns = set(
            connection.read_parquet(str(Path(parquet_path).resolve())).columns
        )
        # Older immutable exports predate pricing_context_json. New summaries
        # must keep production and observed services separate even when totals
        # happen to be equal.
        context_columns = [
            name
            for name in ("production_json", "pricing_context_json", "record_scope")
            if name in columns
        ]
        groups = [
            "product_url",
            "source_product_id",
            "currency",
            "configuration_json",
            "verification_status",
            *context_columns,
        ]
        group_sql = ", ".join(groups)
        cursor = connection.execute(
            f"""SELECT {group_sql}, COUNT(*) AS row_count,
                      MIN(TRY_CAST(subtotal AS DECIMAL(38, 8))) AS min_subtotal,
                      MAX(TRY_CAST(subtotal AS DECIMAL(38, 8))) AS max_subtotal,
                      SUM(CASE WHEN subtotal IS NOT NULL AND TRY_CAST(subtotal AS DECIMAL(38, 8)) IS NULL THEN 1 ELSE 0 END) AS invalid_price_rows
               FROM read_parquet(?)
               GROUP BY {group_sql}
               ORDER BY {group_sql}""",
            [str(Path(parquet_path).resolve())],
        )
        names = [description[0] for description in cursor.description]
        return [
            {
                name: (
                    str(value)
                    if name in ("min_subtotal", "max_subtotal") and value is not None
                    else value
                )
                for name, value in zip(names, values)
            }
            for values in cursor.fetchall()
        ]
