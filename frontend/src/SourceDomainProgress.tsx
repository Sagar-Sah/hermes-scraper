import { useEffect, useState } from 'react';
import { TableProperties, TriangleAlert } from 'lucide-react';
import { api, count, errorMessage, humanize, type SourceDomainPage, type SourceDomainStatus } from './api';

const validCount = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const displayCount = (value: number | null | undefined) => validCount(value) ? count(value) : 'Unknown';
const activeStage = (stage: SourceDomainStatus) => !stage.run_paused && ['running', 'queued', 'prepared', 'installed', 'pending'].includes(stage.status);
function stageStatus(stage: SourceDomainStatus) {
  if (stage.status === 'requests_drained') return 'Known request stage collected';
  if (['needs_attention', 'failed'].includes(stage.status)) return 'Needs attention';
  if (stage.run_paused || stage.status === 'paused') return 'Paused';
  return humanize(stage.status);
}

export default function SourceDomainProgress({ runId }: { runId: string }) {
  const [data, setData] = useState<SourceDomainPage | null>(null);
  const [error, setError] = useState('');
  const [lastSuccess, setLastSuccess] = useState<Date | null>(null);
  useEffect(() => {
    let disposed = false, inFlight = false, failures = 0, nextDelay = 3000;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    setData(null); setError(''); setLastSuccess(null);
    const schedule = () => {
      window.clearTimeout(timer);
      if (!disposed) timer = window.setTimeout(() => void refresh(), nextDelay);
    };
    const refresh = async () => {
      if (disposed || inFlight) return;
      if (document.hidden) { nextDelay = 15000; schedule(); return; }
      inFlight = true;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 12000);
      try {
        const result = await api<SourceDomainPage>(`/runs/${encodeURIComponent(runId)}/source-domains`, { signal: controller.signal });
        if (!result || !Array.isArray(result.items) || result.items.some(item => !item || typeof item.job_id !== 'string' || typeof item.status !== 'string')) throw new Error('Invalid table collection status response.');
        if (disposed) return;
        setData(result); setError(''); setLastSuccess(new Date()); failures = 0;
        nextDelay = !result.items.length || result.items.some(activeStage) ? 3000 : 15000;
      } catch (failure) {
        if (disposed) return;
        setError(errorMessage(failure)); failures++;
        nextDelay = Math.min(30000, 3000 * 2 ** Math.min(failures, 4));
      } finally {
        window.clearTimeout(timeout); inFlight = false;
        schedule();
      }
    };
    const visible = () => { if (!document.hidden && !inFlight) { window.clearTimeout(timer); void refresh(); } };
    document.addEventListener('visibilitychange', visible);
    void refresh();
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visible); };
  }, [runId]);

  return <section className="panel source-domain-panel" aria-label="Price table collection">
    <div className="panel-top"><div className="panel-title"><TableProperties size={17} /><h2>Price table collection</h2></div><span className="subtle-label">Direct HTTP reads</span></div>
    {error && <div className="efficiency-error" role="status"><TriangleAlert size={14} /><span>{data ? 'Live updates unavailable. Showing last-known table collection.' : 'Table collection status is unavailable.'}{lastSuccess && ` Last updated ${lastSuccess.toLocaleTimeString()}.`} {error}</span></div>}
    {!data && !error && <p className="efficiency-loading">Loading table collection progress…</p>}
    {data && !data.items.length && <p className="source-domain-empty">No price table collection stage is recorded for this run.</p>}
    {data?.items.map(stage => {
      const planned = stage.planned_unique_requests, completed = stage.completed_requests;
      const determinate = validCount(planned) && planned > 0 && validCount(completed);
      const percentage = determinate ? Math.min(100, completed / planned * 100) : 0;
      const tone = error ? 'stale' : stage.run_paused && !['requests_drained', 'needs_attention', 'failed'].includes(stage.status) ? 'paused' : stage.status.replaceAll('_', '-');
      return <article className="source-domain-stage" key={stage.job_id} aria-label={`Table stage ${stage.scope || stage.job_id.slice(0, 12)}`}>
        <div className="source-domain-heading"><strong>{stage.scope || `Stage ${stage.job_id.slice(0, 12)}`}</strong><span className={`status status-${tone}`}>{error ? 'Last known: ' : ''}{stageStatus(stage)}</span></div>
        <dl className="source-domain-metrics">
          <div><dt>Known stage requests collected</dt><dd>{displayCount(completed)} / {displayCount(planned)}</dd></div>
          <div><dt>HTTP attempts / budget</dt><dd>{displayCount(stage.attempts_used)} / {displayCount(stage.attempt_budget)}</dd></div>
          <div><dt>Candidate row counts</dt><dd>See variation plan</dd></div>
          <div><dt>Complete catalog</dt><dd>Unknown</dd></div>
        </dl>
        {determinate && <div className="source-domain-track" role="progressbar" aria-label={`${error ? 'Last-known ' : ''}known request stage progress`} aria-valuenow={completed} aria-valuemin={0} aria-valuemax={Math.max(planned, completed)} aria-valuetext={`${count(completed)} of ${count(planned)} known requests collected`}><div style={{ width: `${percentage}%` }} /></div>}
        <p className="source-domain-note">{displayCount(stage.remaining_unique_requests)} known requests remain{validCount(stage.task_counts?.failed) && stage.task_counts.failed > 0 ? ` · ${count(stage.task_counts.failed)} failed` : ''}. Attempts include retries and session bootstrap; they can exceed the unique request count.</p>
        {!!stage.task_errors?.length && <details className="source-domain-errors"><summary>Request errors ({count(stage.task_errors.length)})</summary>{stage.task_errors.map(item => <p key={item.request_key}>{item.error}</p>)}</details>}
      </article>;
    })}
    <p className="source-domain-footnote">This stage reads source tables directly over HTTP; product-by-product browser navigation is not required. Collected request stages and generated candidate matrices are separate from saved product extraction rows. Request-stage progress does not establish complete product pricing, all valid configurations, or verified prices.</p>
  </section>;
}
