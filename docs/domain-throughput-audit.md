# Domain queue throughput and refresh cost

Read-only audit of stage `a8b567e6a120bfdfcd4dd709a3fd55c591e37679de8f2c5448eba7d62f446d08`, followed by a local scheduler correction. The audit and tests made no supplier requests. The stage finished all 7,220 reads without a restart or failures. The corrected scheduler was loaded only after collection completed.

## Measured bottleneck

At the sample, 1,723 attempts had returned HTTP 200. The shared source limiter was already at its configured 6 requests/second, with no backoff. The latest 1,000 successful responses completed at **4.223 requests/second**. Their recorded HTTP durations averaged **51.7 ms**, with **77.7 ms at the 95th percentile**. These durations include the HTTP client/body interval; they do not separately measure network transit.

Each of two workers completed a request approximately every **472 ms**. `SourceDomainCoordinator._pump` previously performed one task, checked status, and then always slept **250 ms**, including when more work was ready. This deliberate delay occupied approximately 53% of each worker cycle. Supplier latency alone cannot explain the observed throughput.

Read-only SQL samples also found about 7.1 ms for a task-status aggregate, 5.1 ms for the error scan, 2.3 ms for job columns, and 4.0 ms for selecting a claim. There are two status checks per task. These scans and the multiple durable writes add overhead, but the audit did not instrument every persistence operation; the remaining cycle time must not be attributed entirely to SQLite.

The correction keeps two workers, all worker/run fences, durable checkpoints, retries, and the existing global source limiter. It sleeps only when an entire pass claims no work. Delayed retries, foreign leases, and empty active sets retain idle polling. Work remains bounded by the source limiter; **6 requests/second is a ceiling, not a demonstrated new rate or an end-to-end ETA**. Database overhead and latency outliers may keep actual throughput lower. No extra workers or cache rules were introduced.

Regression coverage includes a ready three-request queue with a 60-second idle poll setting: it drains without the idle delay while retaining the two-per-second fixture limiter. Separate cases prove delayed retries, foreign leases, and an empty active set do not spin or issue requests. The completed live stage used the old scheduler: its measured collection window was 1,690.414 seconds, or 28.17 minutes, at 4.27 successful response bindings per second. A new live supplier stage has not been run to benchmark the correction.

## Why this stage contains 7,220 reads

| Known request family | Club Flyers | Roll Labels |
| --- | ---: | ---: |
| Specifications | 1 | 1 |
| Exact selector/mode lookups | 570 | 729 |
| Base option catalogs | 304 | 459 |
| Base quantity support | 435 | 729 |
| Option quantity support | 480 | 729 |
| Base and option price tables | 1,406 | 1,377 |
| **Total** | **3,196** | **4,024** |

These are deduplicated table/domain reads, not one quote request per expanded configuration. They cover the known saved selector domain; they do not prove that no new source products, option types, or service combinations exist.

**7,220 is the current fresh-context manifest size, not an intrinsic minimum for these products.** Before the session replacement, the known missing-table manifests totaled 3,475 reads: 2,017 for Club Flyers and 1,458 for Roll Labels. Starting a new context added 3,745 refresh reads: 2,064 specification/lookup/catalog reads and 1,681 previously captured price-table routes. Existing responses remain historical and have not been assigned a new provenance context.

## Future cache boundary

Today, request identity includes the full session scope, source epoch, observed controller context, store, currency, and service context. Losing the in-memory session therefore prevents automatic reuse under a replacement session. This is conservative bookkeeping overhead; a new login does not by itself establish that every product price changed. It also does not establish that every old price stayed valid.

A future design should distinguish the **physical authentication session** used to send a request from the **semantic pricing/domain context** that determines its meaning. Reuse across sessions would require a route-specific, source-supported dependency certificate, matching proven account/store/currency/service/price-group dimensions, and a defined freshness or conditional-revalidation policy. Unproven dimensions must remain in the key. Matching one baseline quote or matching response hashes is insufficient to certify the whole catalog. This audit makes no such equivalence claim and changes no existing evidence bindings.
