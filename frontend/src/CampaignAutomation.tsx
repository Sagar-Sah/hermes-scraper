import { useCallback, useEffect, useRef, useState } from 'react';
import { LoaderCircle, Pause, Play, RefreshCw, TriangleAlert } from 'lucide-react';
import './CampaignAutomation.css';
import { api, count, dateTime, errorMessage, humanize, post } from './api';

type Job = { id: string; product_id: string; profile_id: string; supplier: string; status: string; attempts: number; progress_count?: number; continuations?: number; polls?: number; reason?: string; result_reference?: string; created_at?: number; updated_at?: string };
type Automation = { campaign_id: string; enabled: boolean; capabilities: { profile_id: string; supplier: string; product_ids: string[]; can_discover: boolean }[]; jobs: Job[]; summary: Record<string, number>; worker_faults?: { reason?: string }[]; supplier_blocks?: Record<string, string> };
type Plan = { plan_id: string; jobs: Pick<Job, 'product_id' | 'profile_id' | 'supplier'>[]; blockers: { product_id: string; reason: string }[]; expires_at?: string };

const reasonText = (reason: string) => ({
  needs_profile: 'Supplier collection support needs configuration.',
  catalog_not_available: 'A compatible supplier quote profile or saved option catalog is needed.',
  existing_selection_preserved: 'This task is already recorded; its saved work is preserved.',
  already_recorded: 'This task is already recorded; its saved work is preserved.',
  product_outside_active_scope: 'Waiting for its campaign group.',
  supplier_authentication_required: 'Sign in through the supplier’s Hermes browser.',
  source_project_details_required: 'The supplier’s project details need attention. The signed-in session is retained.',
  source_account_profile_action_required: 'Complete the supplier’s required account setup in its Hermes browser.',
  worker_interrupted: 'Interrupted work needs a result check before it can be repeated.',
  supplier_profile_stage_held: 'The source page could not be verified. Saved work was retained.',
}[reason] || humanize(reason));
const isRetainedPlanItem = (reason: string) => reason === 'existing_selection_preserved' || reason === 'already_recorded';

/** Campaign extraction uses the local worker. No model provider is invoked. */
export default function CampaignAutomation({ campaignId, titles }: { campaignId: string; titles: Record<string, string> }) {
  const [state, setState] = useState<Automation | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [updated, setUpdated] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const generation = useRef(0);
  const base = `/campaigns/${encodeURIComponent(campaignId)}/automation`;
  const name = (id: string) => titles[id] || `Product ${id}`;

  useEffect(() => { setState(null); setPlan(null); setBusy(''); setError(''); setUpdated(''); generation.current++; }, [campaignId]);
  useEffect(() => {
    let disposed = false, timer: number | undefined;
    const controller = new AbortController();
    const refresh = async () => {
      if (disposed) return;
      try {
        const next = await api<Automation>(base, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
        if (disposed || next.campaign_id !== campaignId) return;
        setState(next); setError(''); setUpdated(new Date().toISOString());
        if (next.enabled || (next.summary.running || 0) > 0 || (next.summary.waiting || 0) > 0) timer = window.setTimeout(refresh, 4000);
      } catch (failure) { if (!disposed) setError(errorMessage(failure)); }
    };
    void refresh();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [base, campaignId, refreshKey]);

  const act = useCallback(async (action: 'plan' | 'start' | 'pause' | 'resume') => {
    const version = generation.current;
    setBusy(action); setError('');
    try {
      if (action === 'plan') {
        const next = await post<Plan>(`${base}/plan`, { max_jobs: 99 });
        if (version !== generation.current) return;
        setPlan(next);
      } else {
        await post(`${base}/${action}`, action === 'start' ? { plan_id: plan?.plan_id } : {});
        if (version !== generation.current) return;
        setPlan(null); setRefreshKey(value => value + 1);
      }
    } catch (failure) { if (version === generation.current) setError(errorMessage(failure)); }
    finally { if (version === generation.current) setBusy(''); }
  }, [base, plan?.plan_id]);

  const disabled = !!busy || !state || !!error;
  const registered = new Set(state?.capabilities.map(profile => profile.profile_id));
  const latest = new Map<string, Job>();
  for (const job of state?.jobs || []) {
    if (!registered.has(job.profile_id)) continue;
    const prior = latest.get(job.product_id);
    if (!prior || (job.created_at || 0) > (prior.created_at || 0)) latest.set(job.product_id, job);
  }
  const currentJobs = [...latest.values()];
  const currentIds = new Set(currentJobs.map(job => job.id));
  const history = (state?.jobs || []).filter(job => !currentIds.has(job.id));
  const retainedPlanItems = plan?.blockers.filter(item => isRetainedPlanItem(item.reason)) || [];
  const planBlockers = plan?.blockers.filter(item => !isRetainedPlanItem(item.reason)) || [];
  const resumable = !!state && !state.enabled && currentJobs.some(job => ['queued', 'waiting'].includes(job.status));
  const jobTable = (jobs: Job[]) => <div className="table-scroll"><table className="data-table"><thead><tr><th>Product</th><th>Supplier task</th><th>Status</th><th>Details</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id}><td>{name(job.product_id)}</td><td>{humanize(job.supplier)}<br /><small>Source options and costs</small></td><td>{humanize(job.status)}</td><td>{job.reason ? reasonText(job.reason) : (job.status === 'running' ? 'Collecting and checking supplier data' : job.result_reference ? 'Source result saved' : 'Waiting for worker')}{(job.progress_count || 0) > 0 && <><br /><small>{count(job.progress_count || 0)} saved collection steps</small></>}<br /><small>{count(job.attempts)} {job.attempts === 1 ? 'attempt' : 'attempts'}</small></td></tr>)}</tbody></table></div>;
  return <section className="panel campaign-automation" aria-label="Automatic supplier collection">
    <div className="automation-heading"><div><h2>Automatic supplier collection</h2><p>Hermes collects supported source options and costs, saves results, and moves past products that need attention. No AI provider is used.</p></div><button className="button button-secondary" aria-label="Refresh automatic collection" disabled={!!busy} onClick={() => setRefreshKey(value => value + 1)}><RefreshCw size={15} /></button></div>
    {error && <div className="error-box" role="alert"><TriangleAlert size={16} /><span>{state ? 'Showing last-known worker status. ' : ''}{error}</span></div>}
    {state && <>
      {!!state.worker_faults?.length && <div className="error-box" role="alert"><TriangleAlert size={16} /><span>The worker stopped safely after a storage or execution failure. Saved tasks remain available for review.</span></div>}
      {!!Object.keys(state.supplier_blocks || {}).length && <p role="status">Supplier needs attention: {Object.entries(state.supplier_blocks || {}).map(([supplier, reason]) => `${humanize(supplier)}: ${reasonText(reason)}`).join(' ')} Other suppliers can continue.</p>}
      <dl className="automation-counts" aria-label="Current product task counts">{['queued', 'running', 'waiting', 'completed', 'held', 'access_required'].map(key => <div key={key}><dt>{humanize(key)}</dt><dd>{count(currentJobs.filter(job => job.status === key).length)}</dd></div>)}</dl>
      <div className="automation-actions">
        <button className="button button-secondary" disabled={disabled || state.enabled} onClick={() => void act('plan')}>{busy === 'plan' ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}Check remaining products</button>
        {state.enabled ? <button className="button button-secondary" disabled={disabled} onClick={() => void act('pause')}><Pause size={15} />Pause collection</button> : resumable && <button className="button" disabled={disabled} onClick={() => void act('resume')}><Play size={15} />Resume queued work</button>}
        <span role="status">{state.enabled ? 'Worker enabled' : 'Worker paused'}{updated && ` · Updated ${dateTime(updated)}`}</span>
      </div>
      {plan && <div className="automation-plan" aria-label="Collection plan"><h3>{count(plan.jobs.length)} supported product {plan.jobs.length === 1 ? 'task' : 'tasks'} ready</h3><p>Previously recorded tasks are retained. This plan starts only the supported work listed below.</p><ul>{plan.jobs.map((job, index) => <li key={`${job.product_id}-${job.profile_id}-${index}`}>{name(job.product_id)} · {humanize(job.supplier)}</li>)}</ul>{retainedPlanItems.length > 0 && <><h4>Saved work retained</h4><ul>{retainedPlanItems.map((item, index) => <li key={`${item.product_id}-${index}`}>{name(item.product_id)}: {reasonText(item.reason)}</li>)}</ul></>}{planBlockers.length > 0 && <><h4>Needs attention</h4><ul>{planBlockers.map((item, index) => <li key={`${item.product_id}-${index}`}>{name(item.product_id)}: {reasonText(item.reason)}</li>)}</ul></>}{plan.jobs.length > 0 && <button className="button" disabled={disabled} onClick={() => void act('start')}><Play size={15} />Start {count(plan.jobs.length)} product {plan.jobs.length === 1 ? 'task' : 'tasks'}</button>}</div>}
      {currentJobs.length > 0 && jobTable(currentJobs)}
      {history.length > 0 && <details><summary>{count(history.length)} earlier tasks preserved</summary><p className="muted">Earlier attempts and their saved results remain in the history.</p>{jobTable(history)}</details>}
      <p className="muted">Task completion covers the recorded source scope. Product finalization, storefront retail prices, and checkout checks remain separate.</p>
    </>}
    {!state && !error && <p role="status"><LoaderCircle size={15} className="spin" /> Loading worker status…</p>}
  </section>;
}
