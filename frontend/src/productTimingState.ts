/** Snapshot totals are server-measured. Only a live active interval may advance locally. */
export interface ProductTimingData {
  schema_version: 1;
  scope?: 'product_worker' | 'campaign_job';
  state: 'active' | 'queued' | 'waiting' | 'paused' | 'attention' | 'finished' | 'unknown';
  active_seconds: number | null;
  queued_seconds: number | null;
  waiting_seconds: number | null;
  paused_seconds: number | null;
  attention_seconds: number | null;
  elapsed_seconds: number | null;
  observed_since: number | null;
  server_now: number;
  active_since: number | null;
  live_until: number | null;
  active_now: boolean;
  complete_history: boolean;
  coverage: 'complete' | 'partial' | 'unknown';
  clock_anomaly: boolean;
}
export const measuredSeconds = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
export function durationText(value: number | null | undefined): string {
  if (!measuredSeconds(value)) return 'Not measured';
  const total = Math.floor(value), hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
  if (minutes) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  return `${seconds}s`;
}
export const monotonicNow = () => typeof performance !== 'undefined' ? performance.now() : 0;
export function validTiming(timing: ProductTimingData | null | undefined): timing is ProductTimingData {
  return !!timing && timing.schema_version === 1 && ['complete', 'partial', 'unknown'].includes(timing.coverage)
    && ['active', 'queued', 'waiting', 'paused', 'attention', 'finished', 'unknown'].includes(timing.state);
}
export function canTickTiming(timing: ProductTimingData | null | undefined, live = true): boolean {
  return validTiming(timing) && live && timing.active_now === true && timing.state === 'active'
    && timing.clock_anomaly === false && timing.coverage !== 'unknown' && measuredSeconds(timing.active_seconds)
    && measuredSeconds(timing.server_now) && measuredSeconds(timing.active_since) && timing.active_since <= timing.server_now
    && measuredSeconds(timing.live_until) && timing.live_until > timing.server_now;
}
export function timingView(timing: ProductTimingData | null | undefined, now: number, receivedAt: number, live = true) {
  const valid = validTiming(timing), known = valid && timing.coverage !== 'unknown';
  const partial = known && (timing.complete_history !== true || timing.coverage !== 'complete');
  let active = known && measuredSeconds(timing.active_seconds) ? timing.active_seconds : null;
  let delta = 0;
  const eligible = canTickTiming(timing, live);
  const heartbeatExpired = eligible && Number.isFinite(now) && Number.isFinite(receivedAt) && (now - receivedAt) / 1000 >= timing!.live_until! - timing!.server_now;
  if (eligible && Number.isFinite(now) && Number.isFinite(receivedAt)) {
    delta = Math.max(0, Math.min((now - receivedAt) / 1000, timing!.live_until! - timing!.server_now));
    active = active! + delta;
  }
  const elapsed = valid && measuredSeconds(timing.elapsed_seconds) ? timing.elapsed_seconds : null;
  const label = active === null ? 'Active time not measured' : `${partial ? 'At least ' : ''}${durationText(active)} active`;
  const states = new Map([['active', 'Collecting'], ['queued', 'Queued'], ['waiting', 'Waiting'], ['paused', 'Paused'], ['attention', 'Needs attention'], ['finished', 'Finished'], ['unknown', 'Timing not recorded']]);
  const state = valid ? states.get(timing.state)! : 'Timing not recorded';
  const caption = [!live ? 'Last known' : heartbeatExpired ? 'Awaiting worker update' : state, elapsed !== null ? `${durationText(elapsed)} elapsed` : ''].filter(Boolean).join(' · ');
  const lower = (value: number | null) => measuredSeconds(value) ? `${partial ? 'At least ' : ''}${durationText(value)}` : 'Not measured';
  const details: [string, string][] = [
    ['Active collection', active === null ? 'Not measured' : `${partial ? 'At least ' : ''}${durationText(active)}`],
    ['Queued', known ? lower(timing.queued_seconds) : 'Not measured'],
    ['Waiting between stages', known ? lower(timing.waiting_seconds) : 'Not measured'],
    ['Paused', known ? lower(timing.paused_seconds) : 'Not measured'],
    ['Needs attention', known ? lower(timing.attention_seconds) : 'Not measured'],
    ['Elapsed wall time', durationText(elapsed)],
  ];
  const explanation = 'Active collection is time owned by a worker, including source requests, supplier waits and network waits. It excludes time queued, paused, waiting between stages, or needing attention. Elapsed is wall time, not scraping time. Separate tasks may overlap; their times are not added together.';
  const history = !known ? 'Earlier active time was not recorded.' : partial ? 'Earlier activity was not fully timed; these are measured lower bounds.' : (valid && timing.scope === 'campaign_job' ? 'Cumulative measured time for this campaign task, including its recorded chunks. It may overlap an underlying harvest product.' : 'Cumulative measured time for this harvest product, including its recorded worker intervals.');
  const warning = valid && timing.clock_anomaly ? 'A clock change was detected; live timing is held.' : heartbeatExpired ? 'Live timing is capped until the next worker update.' : '';
  return { active, delta, elapsed, heartbeatExpired, partial, label, caption, state, details, explanation, history, warning,
    tooltip: `${label}. ${caption}. ${explanation} ${history}${warning ? ' ' + warning : ''}` };
}
