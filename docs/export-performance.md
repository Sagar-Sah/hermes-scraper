# Export performance — 14 October 2026

The exporter now computes each audit and customer projection once per source
row. Multi-product exports reuse those projections for their combined and
per-product datasets. The previous implementation projected customer values
three times and audit values twice per row for a multi-product export.
Single-product exports also avoid writing an unused second scratch copy.

## Local measurements

These measurements use **synthetic rows and zero supplier requests**. They
measure local CSV/Parquet publication, not supplier extraction, website timing,
price validity or a full campaign's completion time.

Hardware: Apple M1 Max, 32 GiB RAM, macOS 27.0.1 arm64, Python 3.11.3, PyArrow
25.0.1. Baseline: repository commit `a01ac7d`. Each case ran three times against
a new temporary output directory. The timed region includes fixture generation,
scratch I/O, both output formats and atomic publication with file/directory
syncs; independent verification is outside the timed region.

| Synthetic workload | Baseline median | Optimized median | Less elapsed time |
| --- | ---: | ---: | ---: |
| 30,000 rows, 10 products | 4.704 s | 3.870 s | 17.7% |
| 30,000 rows, 1 product | 2.717 s | 2.582 s | 4.9% |

All baseline and optimized runs had identical combined/per-product CSV bytes
and decoded Parquet records, including source IDs, prices, production, evidence,
full records and corrected display labels. The benchmark reports a SHA-256 over
those contents. See [raw timings and content hashes](export-performance-20261014.json).
These are small local workloads; filesystem cache and concurrent machine load
can change timing.

## Behavior and tradeoff

The first pass still discovers all dynamic columns and immediately rejects
invalid or nonfinite subtotal/unit-price amounts. The combined writer projects
each JSON-round-tripped source row, preserving the existing Decimal/date
conversion. When multiple products exist, a bounded 32-handle LRU writes those
projections into per-product scratch files. Product writers consume the saved
projection without recomputing its prices, service context or display labels.

Memory remains bounded by the Parquet batch, one source/projection row and
product/column metadata. Multi-product scratch projections can occupy more disk
than raw JSON rows because they include both audit and customer views. Scratch
files are removed before publication, and no new persistent cache is introduced.
The generation rename, syncs and final atomic manifest pointer are unchanged.
Failures close scratch handles and retain the previous published generation.
Source records, record scope, provenance, resume state and supplier rate limits
are unchanged.

Regression coverage checks one projection per row, combined/per-product content
and partition order (including over 32 products), dynamic columns, Decimal/date
conversion, invalid/NaN/infinite amounts, a failure after partition writing,
previous-manifest preservation, legacy projections, service scope and engine
resume. The focused export/engine suite passed 68 tests.

## Reproduce

From the repository root, after installing the locked dependencies and editable
backend package into `.venv`:

```sh
git show a01ac7d:backend/crawler/exporter.py > /tmp/hermes-exporter-before.py
.venv/bin/python backend/benchmarks/export_rows.py --rows 30000 --products 10 --repeat 3 --baseline-file /tmp/hermes-exporter-before.py
.venv/bin/python backend/benchmarks/export_rows.py --rows 30000 --products 10 --repeat 3
.venv/bin/python backend/benchmarks/export_rows.py --rows 30000 --products 1 --repeat 3 --baseline-file /tmp/hermes-exporter-before.py
.venv/bin/python backend/benchmarks/export_rows.py --rows 30000 --products 1 --repeat 3
```
