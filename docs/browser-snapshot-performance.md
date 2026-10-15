# Browser snapshot read benchmark — 14 October 2026

The browser snapshot now reuses visibility, labels, selector ancestor paths and sibling indexes within one synchronous DOM observation. Each subsequent observation starts with empty caches. This removes repeated layout and ancestor scans without reusing a previous quote, option list, visibility result or login state. Cleaned body text is also reused within the same observation.

A local Chromium benchmark compared baseline `a01ac7d` against the optimized script. Both scripts returned identical complete snapshot JSON on both synthetic fixtures. The benchmark alternated execution order, discarded four warmup trials, and measured 15 trials of 20 synchronous snapshots each. Fixtures were loaded with `page.set_content`; there were **zero supplier requests**.

| Fixture | Product controls | Radio choices | Baseline median/read | Optimized median/read | Speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small | 8 | 24 | 0.280 ms | 0.190 ms | 1.47× |
| Large | 82 | 640 | 9.060 ms | 4.515 ms | 2.01× |

These figures measure DOM snapshot work in Chromium on this workstation. They exclude browser startup, Playwright transport, database work, supplier response time, and supplier pacing. They are not an end-to-end scraping speed claim.

Run from the repository root:

```sh
PYTHONPATH=backend .venv/bin/python backend/benchmarks/browser_snapshot.py --baseline a01ac7d
```

[Saved results](browser-snapshot-benchmark-20261014.json). Regressions in `backend/tests/test_browser_snapshot_reads.py` check exact selector paths, live changes to control visibility/labels/structure/options/prices between reads, and login/challenge text. Existing service-context tests also ensure collapsed product services and unrelated private inputs remain handled as before.
