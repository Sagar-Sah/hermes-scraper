"""Local-only export benchmark. Generates synthetic rows; never contacts suppliers.

Run from backend with ``python benchmarks/export_rows.py --rows 30000``.
Use --baseline-file with a saved exporter.py to compare the same workload.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq

from crawler import exporter


def fixture_rows(count, products):
    for index in range(count):
        yield {
            "product_url": f"https://fixture.test/product/{index % products}",
            "source_product_id": str(index % products),
            "title": f"Synthetic product {index % products}",
            "configuration": {
                "size": {"label": "3.5 × 2 inches", "value": "sz-1"},
                "stock": {"label": "16 pt Premium Matte", "value": "stock-1"},
                "roundedCorners": {"label": "Round Corners (+ $5.00)", "value": "2"},
                "variant": index,
            },
            "quantity": (index % 10 + 1) * 50,
            "currency": "USD",
            "subtotal": "19.99",
            "unit_price": "0.01999000",
            "production": {"label": "2–4 Business Days", "value": "regular", "status": "available"},
            "pricing_context": {"fixture": True},
            "provenance": "source_table",
            "verification_status": "fixture_verified",
            "evidence_ids": [f"synthetic-{index}"],
            "fixture": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--products", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--baseline-file", type=Path)
    args = parser.parse_args()
    if args.rows < 1 or args.products < 1 or args.repeat < 1:
        parser.error("rows, products, and repeat must be positive")
    implementation = exporter
    if args.baseline_file:
        spec = importlib.util.spec_from_file_location("crawler._export_baseline", args.baseline_file)
        implementation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(implementation)
    timings, signatures = [], []
    with tempfile.TemporaryDirectory(prefix="hermes-export-benchmark-") as scratch:
        for attempt in range(args.repeat):
            start = time.perf_counter()
            manifest = implementation.export_rows(
                fixture_rows(args.rows, args.products), Path(scratch) / str(attempt)
            )
            timings.append(time.perf_counter() - start)
            signature = hashlib.sha256()
            for item in manifest["items"]:
                path = Path(item["path"])
                signature.update(item["name"].encode())
                # Compare decoded Parquet records; file metadata may differ by
                # library version while audit values must remain identical.
                if item["format"] == "parquet":
                    for batch in pq.ParquetFile(path).iter_batches():
                        signature.update(json.dumps(batch.to_pylist(), sort_keys=True, ensure_ascii=False).encode())
                else:
                    signature.update(path.read_bytes())
            signatures.append(signature.hexdigest())
    print(json.dumps({
        "workload": "synthetic_export_only_zero_supplier_requests",
        "implementation": str(args.baseline_file or Path(exporter.__file__)),
        "rows": args.rows, "products": args.products, "seconds": timings,
        "median_seconds": statistics.median(timings),
        "rows_per_second": args.rows / statistics.median(timings),
        "content_sha256": signatures,
    }, indent=2))


if __name__ == "__main__":
    main()
