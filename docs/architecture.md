# Hermes: implementation architecture

This describes the current implementation, rather than the original aspirational design. Operational setup is in [README](../README.md); measured outcomes are in the [synthetic benchmark](benchmark.md) and [live probe](live-probe.md).

## Service and data flow

```text
React dashboard / REST client
  → FastAPI JobSpec validation + optional instruction-to-filter translation
  → persisted Run and Product jobs
  → Engine product leases
  → browser, Printograph API, WooCommerce, or synthetic adapter
  → retained source responses + immutable configuration records
  → per-product CSV/Parquet + manifests
  → combined catalog export → authenticated downloads
```

One FastAPI process starts one background engine. SQLite WAL is the local default; SQLAlchemy also supports PostgreSQL. Use one Uvicorn worker per instance. The dashboard polls actual state, retains partial-coverage distinctions, and excludes synthetic rows from supplier-efficiency metrics.

The engine allows 100 active product tasks by default, configurable up to 256 **per engine instance**. Browser extraction separately limits active product tabs with `ATLAS_BROWSER_PRODUCT_CONCURRENCY` (default 1). A value of 3 allows the three supplied products to progress together in the same saved browser context without raising the shared source request limits. Exports have a two-task semaphore and image work a four-task semaphore. There is no overall run deadline; individual requests, inactivity detection, and optional explicit work bounds remain finite.

## Durable work, pacing, and snapshots

Product claims use 60-second leases and increasing epochs. Heartbeats renew every 15 seconds. PostgreSQL claims use `FOR UPDATE SKIP LOCKED`; SQLite uses immediate transactions. Epoch checks fence superseded workers from publishing rows. Claim ordering follows run creation and product identity; no priority planner is implemented.

Transient network/rate-limit failures and classified HTTP 500/502/503/504 failures can schedule durable retries. Queued `lease_until` is a not-before time. An atomic retry event and epoch change fence the failed worker; the same durable row count permits three recoveries with 5/10/20-second delays. Shared supplier cooldowns still apply. Authentication, schema and price-integrity failures remain explicit failures.

Response bodies are stored as content-addressed objects with database metadata, fingerprints, and evidence IDs. HTTP request keys include method, URL, arguments, pricing context, source profile, credentials generation, and adapter version. Database request leases suppress concurrent equivalent fetches; fresh cached responses can be reused, and expired or explicitly refreshed responses use conditional requests when the source provides validators.

That response-cache path belongs to the HTTP transport. Browser observations retain fingerprints and evidence, but they do not currently suppress equivalent requests issued by the source page. The [live throughput diagnosis](throughput-diagnosis.md) separates this limitation from measured supplier behavior.

Origin pacing and concurrency permits live in the database. Workers share source ceilings and `Retry-After` backoff; healthy responses can gradually recover the rate. Browser and HTTP extraction use the same finite source-profile ceiling validation, with defaults of two requests per second and two in flight. Raising a ceiling preserves the current adaptive rate instead of immediately jumping to the ceiling. These mechanisms do not establish a measured sustainable supplier rate or a complete pre-harvest request count.

Rows checkpoint individually for browser extraction and in batches of 500 for other adapters. The compressed row object is written before its database batch reference is committed. A crash can leave an unreferenced object or replay a request/control sequence; committed row identities remain deduplicated. This is not exactly-once network execution.

Browser traversal additionally journals completed supported finite branches after their rows are durable. Markers are scoped to the run, product, extraction specification, and semantic adapter code; stale workers cannot write them. Resumption rediscovers the top-level domain and skips visited completed branches without re-quoting their rows. Historical observations remain historical, not newly source-verified. The unfinished branch can replay, and rows from before the journal existed do not imply completed branches. Known collapsed-service and unselected custom-size gaps remain explicit partial-coverage reasons. Concrete missing controls, failed restoration, limits, or disappeared journaled descendants cannot silently become complete branches.

Configuration identity uses source selection values, quantity, currency, production identity, and pricing context. Resuming validates stored identities; revisited quotes are compared to their historical prices, while journal-skipped branches retain their prior observations. Conflicting prices stop the product with `snapshot_changed` instead of overwriting old records; disappearing configurations also stop a resumed extraction when a complete domain was claimed. Start a new refresh run for a new price snapshot. Source prices are observed over time, so this is not a guaranteed atomic supplier snapshot.

Browser schema 2 also preserves raw observed proof/file-delivery selections and a canonical service signature. Missing legacy diagnostics remain unknown. Historical unscoped rows stay immutable and are separated from current analysis/exports by `record_scope.py`; per-product classification counts are stored separately. Journal v2 markers include the observed service signature. Unknown or changed signatures cannot promote a completed parent or authorize a cross-context skip. Code/scope changes can require replay; old rows do not substitute for newly required evidence.

## Extraction paths and the guarded bulk preview

- **Browser:** normal Chromium operation inspects visible dependent controls, reads back selections and displayed subtotals, and preserves relevant DOM/network observations. By default, the engine uses a saved native Chromium profile per origin under the runtime `browser-profiles/` directory. Duplicate product tabs share that profile's browser context, retaining login across orderly browser restarts. `ATLAS_BROWSER_PROFILE_DIR` selects an exact profile directory; `ATLAS_BROWSER_PERSIST_SESSION=false` opts into memory-only sessions. Login retention uses Chromium's native profile rather than cookie export/import. Profiles are private runtime data and excluded from deliverables; supplier logout and session expiry still apply. Unknown controls or incomplete domains remain visible in coverage.
- **Printograph API:** explicit optional adapter for specification, product, quantity, base-price, and option-price routes. The public API probe required authorization. It is separate from ordinary browser capture; generic live composition is not assumed proven.
- **WooCommerce:** embedded variation data or supported public variation requests, retaining exact option/variation identities. Hidden variations and quantity-discount completeness are not inferred.
- **Fixture:** deterministic synthetic rows with explicit fixture provenance; no supplier network requests.

`browser_bulk_quantities` defaults to false. Its guarded Business Cards, Club Flyers, and Roll Labels preview binds captured lookup, support, base-price, and option-price responses to the current source origin, product, selections, and request/selection epochs. Unsupported combinations, incomplete tables, ambiguous IDs, changed responses, or quote mismatches fall back to direct browser quoting.

These forms can reveal Orientation only after a first Size selection. The walker makes one observed finite-size selection to discover that dependency before fixing Size in the traversal prefix. It then enumerates size choices under each observed orientation. This temporary discovery selection publishes no row and does not relax output filters; square branches that never expose Orientation proceed after the single attempt.

Square Roll Labels can bind an absent Orientation to explicit same-navigation square dimensions and orientation metadata from the source specification response. That dimension-only response may arrive before form product/store attributes exist. Unknown request metadata remain unknown, explicit conflicts are rejected, and the final snapshot must still establish the supported product and broker context. This exception does not relax the required request metadata for price captures, create a visible Orientation attribute, or waive the displayed subtotal checks.

A non-Roll form may expose an empty-value `Matte Aqueous Coating` selection. Only its exact selected label and unique enabled, non-placeholder choice can enter the baseline candidate path; coating presence and value must also match each captured request context. The parser does not add a coating option charge for that candidate. Both displayed subtotal checks are still mandatory, and unexplained differences trigger direct quoting. Gloss Aqueous is not included in this exception.

Two displayed quantity subtotals must match the reconstructed table before candidate rows publish. Direct samples retain their observed provenance; other tiers remain `source_table` / `pending`, with production marked as requiring a configuration quote. Two samples do not verify every tier, the whole pricing formula, or cart totals. See the README for the currently supported configuration and the recorded live findings.

## Publication, models, and commerce

Per-product exporters spool records and write versioned CSV/Parquet files with source-specific attribute columns and raw record/evidence fields. Manifests publish completed generations. DuckDB combines product datasets into versioned catalog exports; final export work has a separate run state. Image downloads retain associations and manifests. S3 can mirror response objects, but row/export artifacts still require the local/shared data directory.

Table-derived rounded-corner labels use the quantity-neutral name `Round Corners`. A monetary annotation copied from the sampled quantity is retained separately as `source_label`; the row's exact surcharge remains in its pricing components. The API and CSV/Parquet projections apply this correction to existing records without rewriting immutable checkpoints or source responses. Directly observed labels remain unchanged.

Models are optional and currently translate extraction instructions into structured filters. Supported provider shapes include OpenAI-compatible, Anthropic, and configurable JSON APIs. They do not generate prices, autonomously develop adapters, or perform background anomaly analysis. Credentials are encrypted with a preserved Fernet key.

Commerce helpers compare prices only within matching configuration and financial context. Shipping accepts explicit tariffs and package measurements; production applies declared durations and Printograph calendar rules. The dashboard exposes both as estimates. Live shipping, combined-cart charges, and supplier rush-fee quoting are not integrated. Verification helpers exist, but an automatic cart-verification lane is not implemented.

## Code map and checks

| Area | Files |
| --- | --- |
| API, execution, persistence | `backend/crawler/app.py`, `engine.py`, `store.py` |
| Network pacing, TLS, sessions | `transport.py`, `pacing.py`, `tls.py`, `browser_session.py` |
| Browser branch journal | `browser_progress.py`, `store.py`, `engine.py` |
| Source adapters | `backend/crawler/adapters/`; `browser_tables.py` is the pure captured-table parser |
| Discovery, exports, assets | `discovery.py`, `browser_discovery.py`, `exporter.py`, `catalog_export.py`, `assets.py` |
| Price rules and models | `commerce.py`, `verification.py`, `providers.py` |
| UI and API client | `frontend/src/App.tsx`, `CommerceTools.tsx`, `api.ts` |

From the project root, install the locked backend dependencies and editable package as documented, then run `python -m pytest backend/tests`. Run `npm run build` from `frontend/`. Adapter fixtures cover schema/binding failures without supplier traffic. Real-source pilots should declare filters, optional request/configuration bounds, account context, coverage, and row verification status. The 3-million-row benchmark validates local generation/export capacity only.

PostgreSQL deployment, multi-instance coordination under failures, Docker runtime, S3 failure recovery, and sustained supplier throughput need deployment testing. Other remaining work includes general source adapters, full dependency-aware incremental regeneration, a global minimal-request planner, durable queue priorities, and independent cart verification. Cross-instance product concurrency is not globally capped by the per-engine task setting.
