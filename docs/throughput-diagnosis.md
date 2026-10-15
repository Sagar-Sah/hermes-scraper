# Live three-product throughput diagnosis

Audit: 2026-09-26 UTC. Run: `36c1c4b084664d36b7b32dbca7cf8f08`.

The proposed 24-minute completion time was not validated for this implementation. The browser path does not implement the original whole-catalog minimal-request planner or complete price-table coverage. The original audit also found no durable traversal progress; a subsequently added completed-branch journal is described below.

## Measured causes

- Between 22:25:15 and 22:44:29 UTC on September 25, Business Cards and Club Flyers made 2,434 additional counted requests while adding only nine committed configurations. Roll Labels added 148 rows during that window.
- In the extended audit through 22:59 UTC, one Business Cards base-price URL was captured 52 times with exactly one response-body fingerprint. A frequently repeated Club Flyers option-price URL was captured 34 times, also with one fingerprint.
- The traversal called `restore(prefix)` separately for each quantity. Each call received a fresh allowance to reload the page. When the same parent option set could no longer be restored, the crawler repeated that failed recovery for every remaining quantity.
- At the initial audit, browser source traffic was hard-capped at two requests per second and two requests in flight per origin, shared across product tabs. This was an implementation ceiling, not a measured supplier maximum. The larger planning examples assumed a different rate.
- Quantity-table expansion supports selected, validated branches. Other configurations fall back to individual displayed quotes. For example, the square Roll Labels form omits Orientation; the original table binder required that visible control and therefore rejected those branches.
- Before the completed-branch journal, saved row identities prevented duplicate publication but every restart began browser traversal again. Deduplication occurred after repeated browsing and quoting.
- At 00:14:53 UTC on September 26, the local API was offline. Its last saved work was 22:59:07 UTC on September 25. Persisted `running` values were stale and did not establish that any worker was still active.

## Corrective work

The failed-parent branch now exits after its one recovery reload and continues at the ancestor's next branch. It records a partial-coverage gap. It does not claim the skipped combinations were exhausted or clear genuine source errors. A local Chromium regression with 26 quantities verified one recovery reload, retained valid sibling output, and explicit partial coverage. Three focused traversal/error tests passed.

The dashboard now marks cached status as last known when polling fails, preserves cached rows, shows the last successful update, and removes live status styling until connection recovery. The production build and isolated Chromium outage/recovery checks passed.

Square Roll Labels can now use captured quantity tables when the visible form omits Orientation. The binder requires same-navigation product specifications containing exactly one matching square size and its explicit source orientation, then binds the exact product lookup and price tables. The absent control remains absent from exported configurations. Two displayed quote samples are still required; unsupported or conflicting evidence falls back to displayed quotes. Product-wide specifications have their own bounded capture window so long traversals do not evict them. All 117 table tests passed, and Chromium checks covered both rectangular Roll Labels and square labels after 260 intervening ordinary captures.

The service was restored and the run paused while these changes were validated. Its checkpoint contains 2,242 rows: 593 Business Cards, 625 Club Flyers, and 1,024 Roll Labels. No product had exhausted its preset domain. The saved data was independently exported and counted in CSV and Parquet.

After validation, the service loaded the final fixes and the same run resumed at 00:31:06 UTC on September 26. All three product workers started with their existing row checkpoints preserved. The final focused square/Roll test selection passed 22 tests, including the Chromium capture-retention regression.

## Muse test and second deployment

At the user's request, OpenRouter's `meta/muse-spark-1.3-contributor` was tested through the real Hermes instruction interpreter. The first response incorrectly treated “include all available options” as an unsupported enumeration instruction. The prompt now explicitly defines an empty filter object as no restrictions. The second real response returned `filters: {}` and `needs_clarification: false`; that interpretation was attached to the existing run without narrowing its scope. The [test record](muse-spark-test.json) retains both results. All 37 provider regressions passed.

Further corrections deployed at 01:02:11 UTC on September 26, with 2,243 existing rows preserved:

- Browser and HTTP extraction now share source-profile pacing validation. The Printograph profile has a six-request-per-second ceiling and four requests in flight; its existing adaptive rate and 429/503 backoff remain intact. Forty-two pacing/transport/browser tests passed. This ceiling is not a promised achieved rate.
- A durable journal records completed supported finite branches only after their rows commit. Initial adoption cannot infer completed branches from old rows, so one pass is still required to establish markers. Interrupted unfinished branches may replay. Historical skipped observations are explicitly not revalidated, and coverage remains partial when known gaps exist. Forty-four engine/journal/traversal regression tests and two final Chromium resume tests passed.
- High quantities can hide raised-print/spot-coating controls. Restoration can select an observed low quantity to rediscover a valid finishing sibling. A previously observed explicit inactive `None` may disappear at an exact selected quantity; the exported row retains the actual absence. Nonempty finishing selections cannot disappear, and explicit filters are not waived. Both finishing variants passed local Chromium regressions.
- The table binder accepts strictly evidenced inactive `None` spot-coating/raised-print controls, retaining their exact presence in output and request-context matching. Both displayed subtotal gates remain unchanged. All 137 table tests passed; unexplained $3 residuals still fall back to displayed quotes.
- Selected proof/file-delivery context is now retained as diagnostic DOM evidence, outside traversed configurations and price calculations. Six targeted browser tests passed. Historical $3 residuals must not be retroactively labeled as a proof fee without selection evidence.

## Final traversal deployment and measured baseline

The 01:19:29–01:22:42 UTC probe measured 682 counted requests and only 16 new unique rows in 192.44 seconds (3.54 requests/second, 4.99 new rows/minute). Replay and journal creation were excluded from new-row throughput. The saved domain had reached one Business Cards size, one Club Flyers size, and two Roll Labels sizes; the source inventories list 10, 15, and 81 size IDs respectively. These inventory counts are not valid-combination coverage percentages.

The final changes loaded at 01:27:55 UTC on September 26. The same run resumed with all 2,311 saved rows, its existing supplier browser profile, and no attribute filters or explicit request/configuration ceiling:

- Discover late Orientation before fixing Size in the traversal prefix. Three Chromium regressions cover all expected rectangular size/orientation pairs, square sizes, an explicit vertical-only filter, and journal resume.
- Scope an inactive finishing witness to the observed size/paper/color/orientation. Both high-quantity finishing regressions passed.
- Recover an offered Regular domain before an unfixed finishing sibling when a retained Rush mode hides it. Three Chromium regressions verified paid Regular recovery, no override of a fixed Rush prefix, and no Regular action under a Rush-only filter. Missing controls in a fixed production branch remain partial coverage.
- Retain square dimension specifications that arrive before form product/store metadata. Unknown metadata stay unknown; explicit conflicts, query ambiguity, wrong origin/navigation, and unsupported dimensions are rejected. Two Chromium tests include early metadata absence and survival across 260 intervening captures. Price capture requirements remain unchanged.
- Permit an exact enabled empty-value `Matte Aqueous Coating` as a baseline candidate. It retains coating presence and exact request context and adds no guessed surcharge. Both displayed subtotal gates still apply. The success and $3-mismatch Chromium regressions passed; Gloss Aqueous remains unsupported because saved evidence has unexplained charges.

All 161 captured-table parser tests and the Python compilation check passed. These are implementation checks, not evidence that the live catalog has completed. Code changes intentionally invalidate prior journal scopes; saved rows are retained, while new traversal semantics must establish their own completed branches. The local worker remains responsible for completing the run after the interactive test.

The first deployment probe ended at 01:32:32 UTC. All three workers had fresh leases, 64 quantity-table expansions were observed, and ten new-scope journal markers covered 998 existing rows. The job made 646 additional requests but added no unique rows in that window: the saved total remained 2,311. No product or captured supplier HTTP errors appeared. This validates active execution and checkpoint adoption, not a new-size transition, square fast-path live success, or product completion. The [test record](muse-spark-test.json) distinguishes this replay from new output.

At 01:37:33 UTC, the extended probe confirmed **30 genuinely new Roll Labels rows for size ID743**, beyond the previously saved sizes742/448. Roll increased from1,024 to1,054 rows and the run reached2,341 unique rows. Two fresh table expansions produced those rows. All workers remained active without reported worker or captured supplier HTTP errors. This is live evidence of the next-size transition; it does not establish Business Cards/Club Flyers next-size advancement, square metadata success, or full catalog completion. Table-derived rows remain pending verification.

A subsequent immutable progress export contains **2391 rows**. CSV and Parquet were independently counted, committed configuration IDs are unique, and the export is explicitly marked incomplete. No product was complete at the snapshot.

## Remaining limits

Eliminating repeated failures does not establish a new completion ETA. A reliable estimate still requires measured new-row throughput and a defensible remaining-domain count. Sampled table matches do not establish independent cart verification or complete shipping, proof/design service, or custom-dimension coverage. The local synthetic benchmark measures a different workload and must not be used as live supplier throughput.

Optional language-model credentials configure instruction interpretation. The current pricing/traversal path does not call the model, so changing the model does not remove these bottlenecks.
