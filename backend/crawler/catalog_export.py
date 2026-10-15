"""Combine published products without re-running their extraction or expansion."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from .store import uid
from .exporter import _normalize
from .customer_csv import audit_record, format_schemas, write_customer_csv


def combine_catalog(manifests, output_dir):
    import duckdb

    files = [
        item["path"]
        for manifest in manifests
        for item in manifest.get("items", [])
        if item["name"] == "combined.parquet"
    ]
    if not files:
        return {}
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    generation = root / ("snapshot-" + uid())
    generation.mkdir()
    with duckdb.connect(":memory:") as db:
        db.execute("SET memory_limit='512MB'")
        db.execute("SET threads=2")
        relation = db.read_parquet(files, union_by_name=True)
        if {
            "record_json",
            "configuration_json",
            "provenance_json",
            "attr:roundedCorners",
        } <= set(relation.columns):
            # Legacy files stay immutable. Only matching records cross into
            # Python; all other rows retain the native DuckDB copy path.
            relation.create_view("source_catalog")
            db.execute("""CREATE TEMP TABLE label_projections (
                original VARCHAR PRIMARY KEY, record_json VARCHAR,
                configuration_json VARCHAR, corners_json VARCHAR)""")
            # An independent cursor keeps inserts from invalidating the read.
            reader = db.cursor()
            patches = 0
            try:
                reader.execute(
                    """SELECT record_json FROM source_catalog
                    WHERE provenance_json = '"source_table"'
                      AND json_extract_string(try_cast("attr:roundedCorners" AS JSON), '$.value') = '2'
                      AND regexp_matches(json_extract_string(try_cast("attr:roundedCorners" AS JSON), '$.label'), '^Round Corners\\s*\\(\\s*\\+\\s*\\$')"""
                )
                db.execute("BEGIN TRANSACTION")
                while records := reader.fetchmany(1024):
                    normalized = []
                    for (raw,) in records:
                        row = _normalize(json.loads(raw))
                        normalized.append(
                            (
                                raw,
                                row["record_json"],
                                row["configuration_json"],
                                row["attr:roundedCorners"],
                            )
                        )
                    db.executemany(
                        "INSERT OR IGNORE INTO label_projections VALUES (?, ?, ?, ?)",
                        normalized,
                    )
                    patches += len(normalized)
                db.execute("COMMIT")
            finally:
                reader.close()
            if patches:
                relation = db.sql("""SELECT source.* REPLACE (
                    coalesce(patch.record_json, source.record_json) AS record_json,
                    coalesce(patch.configuration_json, source.configuration_json) AS configuration_json,
                    coalesce(patch.corners_json, source."attr:roundedCorners") AS "attr:roundedCorners"
                ) FROM source_catalog source LEFT JOIN label_projections patch
                    ON source.record_json = patch.original""")
        actual_rows = relation.count("*").fetchone()[0]
        parquet_columns = list(relation.columns)
        relation.write_parquet(str(generation / "catalog.parquet"), compression="zstd")
    # Read every record in bounded batches. record_json carries labels/services
    # that legacy flat schemas omit; no LIMIT or materializing fetchall here.
    import pyarrow.parquet as pq
    def records():
        for batch in pq.ParquetFile(generation / "catalog.parquet").iter_batches(batch_size=1024):
            for row in batch.to_pylist():
                yield audit_record(row)
    csv_columns, csv_count = write_customer_csv(records(), generation / "catalog.csv")
    if csv_count != actual_rows:
        raise RuntimeError("Customer catalog row count differs from audit Parquet")
    manifest = {
        "schema_version": "2.0",
        "generation": generation.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "row_count": actual_rows,
        "columns": csv_columns,
        "schemas": format_schemas(csv_columns, parquet_columns),
        "source_row_count": sum(
            x.get("source_row_count", x.get("row_count", 0)) for x in manifests
        ),
        "legacy_row_count": sum(x.get("legacy_row_count", 0) for x in manifests),
        "scoped_row_count": actual_rows,
        "record_scope": (
            "service_context_v2"
            if all(x.get("record_scope") == "service_context_v2" for x in manifests)
            else "not_assessed"
        ),
        "product_generations": [
            {"generation": x.get("generation"), "row_count": x.get("row_count", 0)}
            for x in manifests
        ],
        "verification_claim": "See each row's provenance and verification status",
        "items": [
            {"name": p.name, "path": str(p), "size": p.stat().st_size,
             "format": p.suffix.lstrip("."),
             "schema": "customer_csv" if p.suffix == ".csv" else "audit_parquet"}
            for p in [generation / "catalog.parquet", generation / "catalog.csv"]
        ],
    }
    with (generation / "manifest.json").open("w") as f:
        json.dump(manifest, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp = root / (".manifest-" + uid())
    with temp.open("w") as f:
        json.dump(manifest, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, root / "manifest.json")
    return manifest
