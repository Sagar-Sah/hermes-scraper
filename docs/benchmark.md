# Local capacity benchmark — 25 September 2026

Both runs used **100 synthetic products × 30,000 rows**, with **zero supplier requests by the fixture adapter**. They measure generation, durable storage, and complete CSV/Parquet publication. They do not measure Printograph throughput or verify supplier prices.

| Measurement | Initial exporter | Export pair reuse |
| --- | ---: | ---: |
| Total rows | 3,000,000 | 3,000,000 |
| Products | 100 | 100 |
| Maximum observed active fixture product tasks | 100 | 99 |
| Generation + product + catalog exports | 454.86 s | **417.42 s (6m 57s)** |
| End-to-end rows/second | 6,595.4 | 7,187.0 |
| Peak sampled API/worker RSS | 773.0 MiB | 919.3 MiB |
| Export files | 402 | 202 |
| Sum of export sizes | 9,848,972,931 bytes | 6,568,967,116 bytes |
| Fixture supplier requests | 0 | 0 |
| Final status | completed | completed |

Each run used Python 3.11.3, macOS-15.5-arm64-arm-64bit, SQLite WAL, and a per-engine limit of 100 active product tasks. Memory was sampled every 5 seconds and includes the API/worker process, not child browser or independent verification processes. Short memory peaks can be missed.

The first run generated all rows by the 103.2-second observation. The second generated all rows by 140.3 seconds while a bounded live browser pilot and tests also ran. The second run reached 99 simultaneously active fixture product tasks because other work shared the same engine limit. The first run demonstrated 100. These are single observed runs under different concurrent loads, not a controlled claim of repeatable speedup.

The original per-product exporter wrote two equivalent CSV/Parquet pairs for a single product. The revised exporter reuses one pair. Both retain separate combined catalog files. The change removed 200 duplicate export files and reduced total export bytes by roughly one third. End-to-end observed elapsed time fell by 37.44 seconds, but concurrent workloads prevent attributing the full timing difference solely to that change.

## Independent output verification

Separate DuckDB scans confirmed **3,000,000 rows, 3,000,000 distinct configuration IDs, 100 products, and exactly 30,000 rows per product** in both exported catalog Parquet files. Every record retained `fixture=true`.

The second Parquet audit took 5.25 seconds in a separate process with a 2 GB DuckDB memory ceiling. A streaming Python CSV reader independently parsed **3,000,000 records** across 19 columns in 18.1 seconds. Verification time and memory are excluded from the benchmark measurements.

Later semantic-identity and same-pass price-conflict checks are covered by regression tests and were added after this benchmark. The reported peak memory and throughput describe the exact runs above; they are not a guarantee for later code, source adapters, or hardware. PostgreSQL, Docker images, multiple server instances, S3 failure recovery, and sustained supplier traffic were not benchmarked here.

## Reproduce

Choose **Fixture benchmark**, 100 products, 30,000 rows per product. Wait for the run to finish publishing its catalog exports. All data remains marked synthetic.

- Initial run: `af206ed87dbd47b688aca5c0076186af` — [raw results](benchmark.json).
- Export pair reuse run: `efc7967554364661b55e0164f0ddfd11` — [raw results](optimized-benchmark.json).
