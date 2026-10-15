# Dashboard and API forensic audit

Audit scope: API status summaries, dashboard polling and failure visibility, retained event history, row scope, price curves, export evidence, and user-facing capability claims. The reference run is `36c1c4b084664d36b7b32dbca7cf8f08`. This audit used local persisted data/API responses and code; its regression tests use local fixtures with no supplier requests.

## Observed baseline

At **2026-09-26 02:15:21 UTC**, the API reported `running`, **5,012 retained rows**, **22,939 requests**, zero completed products, two product errors, partial coverage, and no run-level error:

| Product | Status | Retained rows | Requests | Recorded problem |
| --- | --- | ---: | ---: | --- |
| Business Cards | needs_attention | 693 | 8,229 | Source prices changed during extraction/resume; a new refresh is required to preserve the snapshot. |
| Club Flyers | needs_attention | 730 | 11,225 | Chromium `net::ERR_CONNECTION_RESET`. |
| Roll Labels | running | 3,589 | 3,485 | None at that observation. |

This was an incomplete run, not evidence that all three products were progressing. The latest 250 events were IDs **21676–21925**. Club Flyers' error **21737** was included; the still-relevant Business Cards error **18971** was retained in SQLite but unreachable through the old dashboard history view. The export endpoint returned no files because those products had no published manifests. These are historical observations, not the current live run's counts.

## Defects fixed in this pass

| Finding | Concrete failure | Current implementation |
| --- | --- | --- |
| Mixed-run errors were easy to miss | A run could say Running with `run.error=null` while two products needed attention. The Needs attention filter tested only run status. | `frontend/src/App.tsx:111`, `:120`, and `:197` show product attention counts in run lists, overview, and detail. Filtering includes mixed runs with failed products. Saved rows remain inspectable. |
| Completion counter conflated partial with complete | The API counted both `completed` and `partial` in `completed_products`. | `backend/crawler/app.py:164` counts only completed products. `finished_attempts`, `partial_products`, `active_products`, and `attention_products` are separate. Finished attempts describe products whose current attempt stopped, not a cumulative retry count. |
| Old failure history was inaccessible | The UI replaced its event list with the latest 250; the API had no older-page cursor. The previous forward cursor could skip a burst larger than 250 events. | `backend/crawler/app.py:408` supports bounded `before` pages and ascending `after` pages, plus an attention category. `frontend/src/RunActivity.tsx:5` loads earlier pages and filtered errors/retries/gaps without accumulating the entire event stream in memory. |
| Independent polling failures were hidden | Activity or Exports could fail while successful run polling cleared the shared error. Download/control errors also disappeared on the next successful status refresh. | `frontend/src/App.tsx:197` keeps independent event, export, action, and status errors. Cached results remain labeled incomplete/last-known when their polling fails. Successful status polling does not clear a download/control failure. |
| Export freshness and evidence were unclear | The API returned file names/URLs only; a previously published snapshot could look current during a resumed run. UI copy promised coverage reports that were not produced. | `backend/crawler/app.py:535` returns published row count/time/generation, current retained count/coverage, per-row verification claim, scope, and freshness. Existing immutable generation manifests are downloadable. Freshness compares manifest `source_row_count` (legacy fallback `row_count`) with retained source rows, avoiding a false stale label when current exports intentionally exclude historical records. The UI removed the unsupported coverage-report promise. |
| Historical unscoped prices could look current | Old browser observations did not record the service selection context required by the corrected price identity. Inferring those old selections would fabricate evidence. | `backend/crawler/app.py:78` exposes current-context/historical counts or null when not assessed. `:470` defaults rows to current context; `scope=legacy` and `scope=all` retain audit access. `:637` excludes legacy records from analytics. Current exports omit unassessed historical Printograph snapshots; `scope=all` exposes them explicitly. Raw saved observations are unchanged. |
| Equal price contexts were fragmented in the chart | Display labels, provenance, production display status, and source base IDs split otherwise equivalent quote contexts. Conversely, unknown service selections cannot establish equivalence. | `backend/crawler/app.py:514` provides `commerce.price_comparison_context` in the API projection. `frontend/src/App.tsx:138` groups with this same canonical context, excludes legacy/unbound rows, retains conflicting-quantity warnings, and uses hollow pending versus filled source-observed markers. Mixing provenance never upgrades pending verification. |

Queued future product leases now appear as `retry_at`; matching `product_retry_scheduled` events supply attempt and failure reason. The UI displays those fields without changing control actions or deciding retry policy. Backend retry/durability policy belongs to the engine/store audit.

## Important distinctions and remaining limits

- **Retained record counts are not unique configuration progress.** Historical and current context records can describe the same visible configuration. The UI labels the raw total retained records and exposes scoped/historical counts separately. Uninitialized scope counts are unknown, not zero. The migration does not erase old evidence or retroactively verify it.
- **No supported full-catalog ETA exists.** The API explicitly returns null for `configuration_total` and `eta_seconds`; the UI says those values are not established. The progress bar measures fully completed products only. Neither elapsed time nor a request ceiling establishes the number of valid dependent configurations remaining.
- **Product completion is distinct from verified pricing and full services.** Pending table tiers remain pending. Two displayed quantity samples do not verify every reconstructed tier, cart charges, hidden service branches, custom sizes, or shipping.
- **Exports are publication snapshots.** An active/failed product may have inspectable rows without a finished CSV/Parquet generation. Downloadable manifests identify the available immutable generation; raw response bodies are retained in the private workspace, not automatically packaged as a portable evidence bundle. A stale flag describes changed retained row count; it is not a new supplier freshness check.
- **History is paged, not an always-complete live log.** The latest page is explicitly capped at 250. Earlier retained events and attention events are recoverable. No claim is made that the screen holds every event simultaneously.
- **Price curves use only the current page.** The largest comparable series is shown, with its tier count and page scope. It is not a complete catalog-wide discount chart. Ambiguous same-quantity prices suppress the selected curve. Backend analytics is capped at the first 50,000 current rows and reports that sampling scope.
- **Health means API connectivity.** A successful `/health` or Service connected indicator does not mean all workers or products are healthy; product failures have separate indicators.
- README and architecture documentation already distinguish the synthetic three-million-row export benchmark from live supplier throughput. They also state that models translate extraction instructions, shipping forms calculate explicit estimates, source sessions may expire, general minimal-request planning is not implemented, and independent cart verification remains unimplemented. No current code computes a 24-minute full-run ETA or guarantees undetected access.

## Historical defects already fixed before this audit

The offline/last-known status banner and GET timeouts were already implemented; this pass did not reclassify them as new defects. Rounded-corner display-label projection had also been corrected without rewriting historical records. Currency precision and same-quantity conflict suppression existed before this pass. This pass extends pricing-context parity and failure visibility rather than claiming those earlier fixes again.

## Validation

- `cd frontend && npm run build`: TypeScript and production bundle passed.
- `python -m pytest backend/tests/test_dashboard_api.py backend/tests/test_api.py backend/tests/test_dashboard_ui.py -q`: **19 passed in 6.02 seconds**.
- API regressions cover mixed status counts, queued retry metadata, more than 250 events, older error recovery, current/legacy/all row views before and after scope assessment, immutable manifest download, historical export selection, and raw-versus-scoped export freshness.
- The real Chromium dashboard fixture intercepts every request; external URLs are aborted and no server or supplier browser is used. It checks mixed-run attention filtering, prior failure history, unknown ETA, retries, historical record selection, unified direct/pending tier markers, conflicting quote suppression, stale exports, and independently failing Activity/Exports with preserved cached data and download error.
- Source files and bundle were validated locally. Deployment and the live three-product completion outcome are separate checks owned by the main task; this audit does not claim that the live run has finished.
