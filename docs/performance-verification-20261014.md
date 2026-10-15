# Performance revision verification — October 14, 2026

All **1,729 collected backend tests** are covered by passing runs after the final changes. Tests used isolated temporary data directories and local/mock sources; dedicated supplier profiles and live campaign data were not used.

The broad suite was split by module into four subprocesses, each with a separate `ATLAS_DATA_DIR`, empty browser profile/storage overrides and headless Chromium:

| Shard | First run result | Elapsed |
| --- | --- | ---: |
| 0 | 620 passed | 68.59 s |
| 1 | 346 passed; 2 subtests passed | 449.58 s |
| 2 | 464 passed; 1 dashboard selector failure | 550.92 s |
| 3 | 296 passed; 1 dashboard selector failure | 383.72 s |

The two dashboard failures were strict locator ambiguity: the run name matched both its open button and its delete button. Both tests now target the run-title button. The dashboard, variation-plan UI and new real-browser capacity integration selection subsequently passed **3/3** in 27.88 seconds. The capacity integration was added after the broad run began, accounting for the additional collected test. Thus the 1,726 broad passes plus the two repaired UI cases and one new browser integration cover all 1,729 tests. There were no skipped tests in these runs.

The real-browser test submitted four extraction tasks at global capacity four and source capacity two. Two actual local Chromium product pages were active while two tasks waited; all four produced correct source prices and exact finite coverage. Source reservations, wake events, page ownership, worker counters and global permits were cleaned up. Separate logical queue tests cover 99 products spread across origins, cancellation and interrupted admission. They do not demonstrate hardware capacity for 99 physical pages.

The frontend production build (`npm ci`, `npm run build`), Python compile check and `bash -n scripts/start-local.sh` passed. Independent code review found and resolved an unsafe generic delayed-price shortcut and missing stability-helper journal fingerprinting before packaging. Unknown sources retain original wait behavior; a 1.8-second delayed-price regression passes.

The measured speed results and their limits are in [the update](../UPDATE-2026-10-14.md), [browser measurements](performance-optimized-20261014.json), [snapshot measurements](browser-snapshot-performance.md), and [export measurements](export-performance.md). Supplier pacing, source-price provenance and validation gates remain in place. No live supplier request, running-service restart, database migration or WordPress deployment was part of this verification.
