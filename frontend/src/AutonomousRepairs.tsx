import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, FileCheck2, LoaderCircle, RefreshCw, RotateCcw, ShieldCheck, TriangleAlert, Upload } from 'lucide-react';
import { api, errorMessage, humanize } from './api';
import './AutonomousRepairs.css';

const PREFIX = '/companion/autonomy/repairs';
const RELEASE_CHECKS = ['calculator_data', 'rendered_prices', 'artwork_dimensions', 'shipping', 'customer_flow'];
export interface RepairRecord {
  id: string; status: string; manifest_sha256: string; title?: string; release_ready?: boolean;
  release_hold_checks?: string[]; reason?: string; error?: string; can_rollback?: boolean;
  manifest: { product_id: string; created_at?: number; diff?: unknown[]; changes?: { kind: string; [key: string]: unknown }[];
    source?: { source_observed_at?: number }; production_origin?: string; staging_origin?: string; [key: string]: unknown };
  staging_verification?: { checks?: Record<string, unknown>; [key: string]: unknown };
  production_verification?: { checks?: Record<string, unknown>; [key: string]: unknown };
  production_approval?: { actor?: string; manifest_sha256?: string; approved_at?: number } | null;
  staging_change?: Record<string, unknown>; production_change?: Record<string, unknown>;
  staging_rollback?: { status?: string }; production_rollback?: { status?: string };
}
interface ReleaseConnection { connected?: boolean; configured?: boolean; reason?: string }
type RepairAction = 'recheck' | 'approve-production' | 'promote' | 'rollback-staging' | 'rollback-production';

export function isRepairRecord(value: unknown): value is RepairRecord {
  if (!value || typeof value !== 'object') return false;
  const record = value as Partial<RepairRecord>;
  return typeof record.id === 'string' && /^[a-f0-9]{32}$/.test(record.id)
    && typeof record.manifest_sha256 === 'string' && /^[a-f0-9]{64}$/.test(record.manifest_sha256)
    && typeof record.status === 'string' && !!record.manifest && typeof record.manifest === 'object'
    && /^[1-9][0-9]{0,11}$/.test(String(record.manifest.product_id));
}

function matched(value: unknown) { return value === 'match'; }
export function repairControls(record: RepairRecord | null, connected: boolean, acknowledged: boolean, now = Date.now() / 1000) {
  const observed = record?.manifest.source?.source_observed_at;
  const fresh = typeof observed === 'number' && Number.isFinite(observed) && now >= observed && now - observed <= 6 * 3600;
  const valid = !!record && /^[a-f0-9]{32}$/.test(record.id) && /^[a-f0-9]{64}$/.test(record.manifest_sha256);
  const checks = record?.staging_verification?.checks || {};
  const required = [...RELEASE_CHECKS];
  if (record?.manifest.changes?.some(change => change.kind === 'add_document')) required.push('documents');
  const held = required.filter(key => !matched(checks[key]));
  const holds = [...new Set([...held, ...(record?.release_hold_checks || [])])];
  const ready = valid && fresh && connected && record?.status === 'staging_verified' && record.release_ready === true && holds.length === 0;
  const approved = !!record?.production_approval && record.production_approval.actor === 'human'
    && record.production_approval.manifest_sha256 === record.manifest_sha256;
  const activeProduction = !!record?.production_change && record.production_rollback?.status !== 'verified';
  return { fresh, holds, approved, ready,
    canApprove: ready && acknowledged && !approved,
    canPromote: ready && approved,
    canRecheck: valid && record?.status === 'staging_verified',
    canRollbackStaging: valid && !!record?.staging_change && record.staging_rollback?.status !== 'verified' && !activeProduction,
    canRollbackProduction: valid && connected && activeProduction && approved,
  };
}

/** Synchronous guard: two clicks before React renders still perform one action. */
export class RepairActionGate {
  private active = false;
  async run(work: () => Promise<void>): Promise<boolean> {
    if (this.active) return false;
    this.active = true;
    try { await work(); return true; } finally { this.active = false; }
  }
}

export function repairActionRequest(action: RepairAction, record: RepairRecord) {
  const rollback = action.startsWith('rollback-');
  return { path: `${PREFIX}/${encodeURIComponent(record.id)}/${rollback ? 'rollback' : action}`,
    body: action === 'recheck' ? {} : rollback
      ? { environment: action === 'rollback-production' ? 'production' : 'staging', manifest_sha256: record.manifest_sha256 }
      : { manifest_sha256: record.manifest_sha256 } };
}

export function RepairReleaseDetails({ record, connected, acknowledged, busy, onAcknowledge, onAction }:
  { record: RepairRecord; connected: boolean; acknowledged: boolean; busy: boolean;
    onAcknowledge: (value: boolean) => void; onAction: (action: RepairAction) => void }) {
  const controls = repairControls(record, connected, acknowledged);
  const checks = record.staging_verification?.checks || {};
  const diff = record.manifest.diff || [];
  return <article className="autonomy-repair-detail" aria-label={`Repair for product ${record.manifest.product_id}`}>
    <header><div><h4>{record.title || `Product ${record.manifest.product_id}`}</h4><p>WooCommerce {record.manifest.product_id} · {humanize(record.status)}</p></div><span className="autonomy-pill">{record.status === 'production_verified' ? 'Published and rechecked' : 'Manual release required'}</span></header>
    <p className="autonomy-repair-hash"><strong>Immutable manifest</strong><code>{record.manifest_sha256}</code></p>
    <p>Approval applies to this exact saved difference. Staging verification alone does not mean the product is fully verified or published.</p>
    <details className="autonomy-repair-diff"><summary>Exact field changes · {diff.length}</summary><pre>{JSON.stringify(diff, null, 2)}</pre></details>
    <div className="autonomy-repair-checks" aria-label="Release checks">{[...new Set([...RELEASE_CHECKS, ...Object.keys(checks)])].map(key =>
      <div key={key}><span>{humanize(key)}</span><strong className={matched(checks[key]) ? 'repair-check-pass' : 'repair-check-hold'}>{humanize(typeof checks[key] === 'string' ? String(checks[key]) : 'unknown')}</strong></div>)}</div>
    {controls.holds.length > 0 && <p className="autonomy-repair-hold"><TriangleAlert size={15} />Release held: {controls.holds.map(humanize).join(', ')}.</p>}
    {!controls.fresh && <p className="autonomy-repair-hold">Source evidence is missing or older than six hours. A fresh source audit and a new bound manifest are required before release. Rechecking the storefront does not renew supplier evidence.</p>}
    {(record.reason || record.error) && <p className="autonomy-repair-hold">{humanize(record.reason || record.error || '').slice(0, 500)}</p>}
    <div className="autonomy-actions"><button type="button" className="button button-secondary" onClick={() => onAction('recheck')} disabled={busy || !controls.canRecheck}><RefreshCw size={14} />Recheck staging</button></div>
    <label className="autonomy-repair-ack"><input type="checkbox" checked={acknowledged} disabled={busy || !controls.ready || controls.approved} onChange={event => onAcknowledge(event.target.checked)} /><span>I reviewed the exact changes and this manifest. I approve this repair for production release.</span></label>
    {controls.approved && <p className="autonomy-repair-approved"><Check size={15} />Human approval is recorded for this exact manifest. Publishing remains a separate action.</p>}
    <div className="autonomy-actions"><button type="button" className="button button-secondary" onClick={() => onAction('approve-production')} disabled={busy || !controls.canApprove}><FileCheck2 size={14} />Approve manifest</button><button type="button" className="button button-primary" onClick={() => onAction('promote')} disabled={busy || !controls.canPromote}><Upload size={14} />Promote to main</button></div>
    <div className="autonomy-actions autonomy-repair-rollbacks"><button type="button" className="button button-secondary" onClick={() => onAction('rollback-staging')} disabled={busy || !controls.canRollbackStaging}><RotateCcw size={14} />Rollback staging repair</button><button type="button" className="button button-secondary" onClick={() => onAction('rollback-production')} disabled={busy || !controls.canRollbackProduction}><RotateCcw size={14} />Rollback main repair</button></div>
  </article>;
}

export default function AutonomousRepairs({ releaseConnection }: { releaseConnection?: ReleaseConnection }) {
  const [records, setRecords] = useState<RepairRecord[]>([]);
  const [selected, setSelected] = useState<RepairRecord | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [loading, setLoading] = useState(true);
  const [readingDetail, setReadingDetail] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState('');
  const [confirmation, setConfirmation] = useState<RepairAction | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const gate = useRef(new RepairActionGate());
  const alive = useRef(true);
  const serial = useRef(0);
  const loadList = useCallback(async () => {
    const response = await api<{ items: RepairRecord[] }>(PREFIX);
    if (!Array.isArray(response.items) || response.items.some(record => !isRepairRecord(record))) throw new Error('Saved repair response is incomplete. Refresh after checking the backend.');
    if (alive.current) setRecords(response.items || []);
  }, []);
  useEffect(() => {
    alive.current = true;
    void loadList().catch(value => { if (alive.current) setError(errorMessage(value).slice(0, 500)); })
      .finally(() => { if (alive.current) setLoading(false); });
    return () => { alive.current = false; serial.current++; };
  }, [loadList]);
  const loadDetail = async (id: string) => {
    const ticket = ++serial.current;
    setSelectedId(id); setSelected(null); setReadingDetail(true); setAcknowledged(false); setConfirmation(null); setError('');
    try {
      const record = await api<RepairRecord>(`${PREFIX}/${encodeURIComponent(id)}`);
      if (!isRepairRecord(record) || record.id !== id) throw new Error('The saved repair identity changed. Reload its manifest before taking action.');
      if (alive.current && ticket === serial.current) setSelected(record);
    } catch (value) { if (alive.current && ticket === serial.current) setError(errorMessage(value).slice(0, 500)); }
    finally { if (alive.current && ticket === serial.current) setReadingDetail(false); }
  };
  const refresh = async () => {
    setLoading(true); setError('');
    try { await loadList(); if (selectedId) await loadDetail(selectedId); }
    catch (value) { if (alive.current) setError(errorMessage(value).slice(0, 500)); }
    finally { if (alive.current) setLoading(false); }
  };
  const blocked = !!busy || loading || readingDetail;
  const connected = releaseConnection?.connected === true;
  const execute = async (action: RepairAction) => {
    const record = selected;
    if (!record || blocked) return;
    const controls = repairControls(record, connected, acknowledged);
    const allowed = action === 'recheck' ? controls.canRecheck : action === 'approve-production' ? controls.canApprove
      : action === 'promote' ? controls.canPromote : action === 'rollback-production' ? controls.canRollbackProduction : controls.canRollbackStaging;
    if (!allowed) return;
    await gate.current.run(async () => {
      setBusy(action); setError(''); setNotice('');
      try {
        const request = repairActionRequest(action, record);
        const value = await api<RepairRecord>(request.path, { method: 'POST', body: JSON.stringify(request.body) });
        if (!isRepairRecord(value) || value.id !== record.id || value.manifest_sha256 !== record.manifest_sha256) throw new Error('The returned repair does not match the reviewed manifest. Refresh before taking another action.');
        if (!alive.current) return;
        setSelected(value); setRecords(previous => previous.map(item => item.id === value.id ? value : item));
        setAcknowledged(false); setConfirmation(null);
        setNotice(action === 'approve-production' ? 'Approval recorded. This has not published the repair.' : `Saved result: ${humanize(value.status)}. Review its checks and any remaining holds.`);
      } catch (value) { if (alive.current) setError(errorMessage(value).slice(0, 500)); }
      finally { if (alive.current) setBusy(''); }
    });
  };
  const requestAction = (action: RepairAction) => {
    if (action === 'recheck' || action === 'approve-production') void execute(action);
    else setConfirmation(action);
  };
  return <section className="autonomy-repairs" aria-labelledby="autonomy-repairs-heading">
    <header><div><h3 id="autonomy-repairs-heading">Staging repairs &amp; manual releases</h3><p>Review saved repairs, their exact differences and unresolved checks. AI cannot approve or publish these changes.</p></div><button type="button" className="button button-secondary" disabled={blocked} onClick={() => void refresh()}>{loading ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}Refresh repairs</button></header>
    {!connected && <p className="autonomy-repair-hold"><ShieldCheck size={15} />{releaseConnection?.reason ? humanize(releaseConnection.reason) : 'Production release connection is not verified. Publishing and production rollback are unavailable; staging records can still be reviewed.'}</p>}
    {error && <p role="alert" className="autonomy-message autonomy-error">{error}</p>}
    {notice && <p role="status" className="autonomy-message">{notice}</p>}
    {loading && <p className="autonomy-empty"><LoaderCircle className="spin" size={14} />Loading saved repairs…</p>}
    {!loading && records.length === 0 && <p className="autonomy-empty">No saved staging repairs yet. Product audits and unsupported repair holds remain in their audit cycle.</p>}
    {records.length > 0 && <div className="autonomy-repair-list" aria-label="Saved staging repairs">{records.map(record => <button type="button" key={record.id} className={`autonomy-repair-row${selectedId === record.id ? ' is-selected' : ''}`} aria-pressed={selectedId === record.id} disabled={!!busy} onClick={() => void loadDetail(record.id)}><span><strong>{record.title || `Product ${record.manifest.product_id}`}</strong><small>{humanize(record.status)}</small></span><span>{record.status === 'production_verified' ? 'Published and rechecked' : record.release_ready === true ? 'Review release checks' : 'Release held'}<small>{record.manifest_sha256.slice(0, 12)}…</small></span></button>)}</div>}
    {readingDetail && <p className="autonomy-empty">Reading the saved manifest…</p>}
    {selected && <RepairReleaseDetails record={selected} connected={connected} acknowledged={acknowledged} busy={blocked} onAcknowledge={setAcknowledged} onAction={requestAction} />}
    {confirmation && selected && <div className="autonomy-repair-confirm" role="group" aria-live="polite" aria-label="Confirm repair action"><strong>{confirmation === 'promote' ? 'Publish this approved repair to main?' : `Restore the saved ${confirmation === 'rollback-production' ? 'production' : 'staging'} state for this repair?`}</strong><p>{confirmation === 'promote' ? 'This changes the live product using only the approved manifest. The server checks its source evidence and current revision, then verifies the storefront.' : 'This restores only this repair’s backed-up product data. A conflicting manual edit prevents rollback; it does not restore the whole website.'}</p><code>{selected.manifest_sha256}</code><div className="autonomy-actions"><button type="button" className="button button-primary" disabled={blocked} onClick={() => void execute(confirmation)}>{busy ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{confirmation === 'promote' ? 'Publish approved repair' : 'Restore saved repair state'}</button><button type="button" className="button button-secondary" disabled={!!busy} onClick={() => setConfirmation(null)}>Cancel</button></div></div>}
  </section>;
}
