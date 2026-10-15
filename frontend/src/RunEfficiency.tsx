import { useCallback, useEffect, useRef, useState } from 'react';
import { Network, RefreshCw, TriangleAlert } from 'lucide-react';
import { api, count, dateTime, errorMessage } from './api';

interface Efficiency {
  network_requests: number;
  source_response_observations: number;
  distinct_response_keys: number;
  distinct_semantic_responses: number;
  current_rows: number | null;
  new_rows: number;
  started_at: string | null;
  limits?: string;
  rows_per_request: number | null;
  requests_per_unique_response: number | null;
  attribution_scope: string | Record<string, unknown>;
}
const ratio = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value);

/** Observations, not an estimate of the unvisited domain or a safe reuse budget. */
export default function RunEfficiency({ runId }: { runId: string }) {
  const [data, setData] = useState<Efficiency | null>(null);
  const [error, setError] = useState('');
  const active = useRef(false);
  const version = useRef(0);
  const refresh = useCallback(async () => {
    if (active.current) return;
    active.current = true;
    const current = ++version.current;
    try {
      const result = await api<Efficiency>(`/runs/${runId}/efficiency`);
      if (current !== version.current) return;
      setData(result); setError('');
    } catch (failure) {
      if (current === version.current) setError(errorMessage(failure));
    } finally { if (current === version.current) active.current = false; }
  }, [runId]);
  useEffect(() => {
    setData(null); setError(''); void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 3000);
    return () => { window.clearInterval(timer); version.current++; active.current = false; };
  }, [refresh]);
  const metrics = data ? [
    ['Network requests', count(data.network_requests)],
    ['Response observations', count(data.source_response_observations)],
    ['Distinct response keys', count(data.distinct_response_keys)],
    ['Distinct request + response pairs', count(data.distinct_semantic_responses)],
    ['New records / request', ratio(data.rows_per_request)],
    ['Observations / unique response', ratio(data.requests_per_unique_response)],
  ] : [];
  return <section className="panel efficiency-panel" aria-label="Observed request efficiency">
    <div className="panel-top"><div className="panel-title"><Network size={17} /><h2>Observed request efficiency</h2></div><button className="icon-button" onClick={() => void refresh()} aria-label="Refresh request efficiency"><RefreshCw size={14} /></button></div>
    {error && <div className="efficiency-error" role="status"><TriangleAlert size={14} /><span>{data ? 'Last-known efficiency; live updates unavailable.' : 'Request efficiency is unavailable.'} {error}</span></div>}
    {data ? <><dl className="efficiency-grid">{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><div className="efficiency-notes"><p>{count(data.new_rows)} new retained records{data.started_at ? ` since ${dateTime(data.started_at)}` : " in the telemetry window"}. {data.current_rows == null ? "Current-context record count is not assessed." : `${count(data.current_rows)} current-context records in total.`} New records may revisit a historical configuration.</p><p>Requests and captured response observations are different counters. Matching content alone does not prove responses can be safely reused.</p><details><summary>Measurement scope</summary><p>{typeof data.attribution_scope === 'string' ? data.attribution_scope : JSON.stringify(data.attribution_scope)}</p>{data.limits && <p>{data.limits}</p>}</details></div></> : !error && <p className="efficiency-loading">Loading observed counters…</p>}
  </section>;
}
