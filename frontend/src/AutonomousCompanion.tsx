import { useCallback, useEffect, useRef, useState } from 'react';
import { CalendarClock, CircleHelp, Clock3, Layers3, LoaderCircle, Pause, Play, RefreshCw, Settings2, ShieldCheck, TriangleAlert } from 'lucide-react';
import { api, count, errorMessage, humanize, type Provider } from './api';
import './AutonomousCompanion.css';
import AutonomousRepairs from './AutonomousRepairs';

const PREFIX = '/companion/autonomy';
const POLL_MS = 10000;
export interface AutonomyConfig {
  enabled: boolean; schedule_enabled: boolean; site_url: string; staging_url: string; timezone: string;
  worker_capacity: number; batch_size: number; director_enabled: boolean; director_provider_id: string | null;
  director_budget_usd: string;
}
export interface AutonomousProduct {
  id: string; product_id: string; title: string; supplier?: string | null; source_url?: string | null;
  storefront_url?: string; status: string; stage?: string; reason?: string | null; repair_status?: string;
  result?: { status?: string; complete?: boolean; evidence_complete?: boolean; checks?: Record<string, unknown>; unknown_checks?: unknown[]; [key: string]: unknown } | null;
  timing?: { active_seconds?: number | null; elapsed_seconds?: number | null; [key: string]: unknown } | null;
}
export interface AutonomousCycle {
  id: string; status: string; kind?: string; scheduled_month?: string; created_at?: string | number; updated_at?: string | number;
  baseline?: Record<string, unknown> | null;
  inventory?: { status?: string; count?: number; pages?: number; category_pages?: number; complete?: boolean };
  summary?: Record<string, number>;
  working_window?: AutonomousProduct[]; next_window?: AutonomousProduct[]; products?: AutonomousProduct[];
  supplier_blocks?: Record<string, unknown> | unknown[]; progress?: unknown; timing?: Record<string, unknown>;
  director?: Record<string, unknown>;
}
interface AutonomyStatus {
  enabled?: boolean; schedule_enabled?: boolean; next_run_at?: string | number | null;
  active_cycle_id?: string | null; cycle?: AutonomousCycle | null; config?: Partial<AutonomyConfig>;
  capabilities?: Record<string, unknown>; environments?: Record<string, unknown>;
  maintenance?: boolean; baseline?: BaselineStatus;
  director?: { provider_id?: string | null; cap_usd?: string; spent_usd?: string; reserved_usd?: string; remaining_usd?: string; month?: string; ready?: boolean; reason?: string; preflight_ready?: boolean; preflight_checked_at?: number };
}
interface BaselineStatus { ready?: boolean; reason?: string; id?: string; manifest_sha256?: string; approved_at?: number }
interface BaselineCapture {
  id: string; manifest_sha256: string; captured_at?: number; ready?: boolean;
  production?: Record<string, unknown>; staging?: Record<string, unknown>;
  backups_verified?: boolean; runtime_parity_verified?: boolean; isolation_verified?: boolean;
}
interface DirectorReadiness { ready?: boolean; reason?: string; preflight_ready?: boolean; preflight_checked_at?: number }
interface EnvironmentProfile { configured?: boolean; connected?: boolean; base_url?: string; role?: string; reason?: string; protected_preview_credentials_present?: boolean }
interface EnvironmentProfiles { production?: EnvironmentProfile; staging?: EnvironmentProfile; production_release?: EnvironmentProfile }
interface EnvironmentInput { base_url: string; token?: string; role: 'read' | 'write'; protection_auth?: { username: string; password: string } }
const EMPTY_CONFIG: AutonomyConfig = {
  enabled: false, schedule_enabled: false, site_url: 'https://printitprints.com',
  staging_url: 'https://staging3.tonyl36.sg-host.com', timezone: 'America/Phoenix', worker_capacity: 4,
  batch_size: 99, director_enabled: false, director_provider_id: null, director_budget_usd: '25.00',
};

export function autonomyMoney(value: unknown): string {
  if ((typeof value !== 'string' && typeof value !== 'number') || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0) return 'Not measured';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value));
}
export function autonomyDuration(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Not measured';
  if (value < 60) return `${Math.floor(value)}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
  return `${Math.floor(value / 3600)}h ${String(Math.floor(value / 60) % 60).padStart(2, '0')}m`;
}
function checkedState(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && 'status' in value) return String((value as { status: unknown }).status);
  return 'unknown';
}
export function autonomyProductVerdict(product: AutonomousProduct): { label: string; verified: boolean; unknown: number | null } {
  const result = product.result;
  const values = Object.values(result?.checks || {});
  const listedUnknown = Array.isArray(result?.unknown_checks) ? result!.unknown_checks!.length : null;
  const unchecked = values.filter(value => !['match', 'verified', 'passed', 'not_applicable'].includes(checkedState(value))).length;
  const unknown = values.length || listedUnknown !== null ? Math.max(unchecked, listedUnknown || 0) : null;
  const verified = product.status === 'verified' && values.length > 0 && result?.evidence_complete === true
    && result?.complete !== false && unknown === 0;
  return { label: product.status === 'verified' && !verified ? 'Needs evidence' : humanize(product.status || 'unknown'), verified, unknown };
}
export function isMuseProfile(provider: Pick<Provider, 'name' | 'model'>): boolean { return /muse/i.test(`${provider.name} ${provider.model}`); }
export function autonomyDate(value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number') return 'Not scheduled';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date) : 'Unavailable';
}

/** Recovery only removes the maintenance fence. Held source work is never replayed here. */
export function autonomyRecoveryRequest(maintenance: boolean, acknowledged: boolean) {
  if (!maintenance || !acknowledged) throw new Error('maintenance_recovery_acknowledgment_required');
  return { path: `${PREFIX}/maintenance/end`, method: 'POST', body: '{}' };
}

export function AutonomyMaintenanceNotice({ maintenance, acknowledged, busy, onAcknowledge, onRecover }:
  { maintenance: boolean; acknowledged: boolean; busy: boolean; onAcknowledge: (value: boolean) => void; onRecover: () => void }) {
  if (!maintenance) return null;
  return <div className="autonomy-setup-card autonomy-recovery" role="region" aria-label="Maintenance recovery">
    <h4><TriangleAlert size={16} /> Audit workers are paused for maintenance</h4>
    <p>After the update has finished, clear this pause to permit new coordinator requests. Saved cycles stay paused; uncertain source requests remain held.</p>
    <label className="autonomy-checkbox"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={event => onAcknowledge(event.target.checked)} /><span>The update is finished. I want to clear the maintenance pause without resuming or repeating held work.</span></label>
    <button type="button" className="button button-secondary" disabled={busy || !acknowledged} onClick={onRecover}><ShieldCheck size={15} />Clear maintenance pause</button>
  </div>;
}

export function DirectorReadinessDetails({ saved, checked, busy }: { saved?: DirectorReadiness; checked: DirectorReadiness | null; busy: boolean }) {
  // A previous successful check cannot override the backend's current hold
  // after a profile change, a key rotation or the one-hour validity window.
  const effective = checked?.ready === false && checked.preflight_ready !== true ? checked : saved || checked;
  const reason = effective?.reason || 'director_preflight_required';
  const preflightPassed = effective?.preflight_ready === true && saved?.preflight_ready !== false;
  const rejected = /request_rejected_401|credentials_unavailable/.test(reason);
  return <div className="autonomy-director-readiness" role="status">
    <strong>{busy ? 'Checking saved Muse profile…' : effective?.ready === true ? 'Director ready' : preflightPassed ? `Provider check passed · Director ${reason === 'director_disabled' ? 'disabled' : 'held'}` : 'Director held'}</strong>
    <span>{busy ? 'Read-only provider check; no inference request is sent.' : humanize(reason)}</span>
    {!busy && rejected && <p>Update the saved Muse key in Sources &amp; AI, then check again. Enter it only in the protected provider field.</p>}
    {!busy && /isolated_monthly_key_limit/.test(reason) && <p>Use a dedicated OpenRouter director key with a monthly limit of $25 or less. Hermes does not change a shared key’s limits.</p>}
    {!busy && preflightPassed && reason === 'director_disabled' && <p>The saved key, model and provider spending limit passed. AI reviews remain off until you enable the Muse director and save audit settings.</p>}
    {!busy && effective?.ready !== true && <p>Collection and deterministic checks continue. AI decisions stay held; this check does not start a review or approve changes.</p>}
  </div>;
}

export function AutonomyBaselineDetails({ baseline, capture, connected, busy, onCapture, onRefresh }:
  { baseline?: BaselineStatus | null; capture: BaselineCapture | null; connected: boolean; busy: boolean; onCapture: () => void; onRefresh: () => void }) {
  return <div className="autonomy-setup-card" aria-label="Staging baseline status">
    <h4>{baseline?.ready === true ? <ShieldCheck size={16} /> : <TriangleAlert size={16} />}{baseline?.ready === true ? 'Approved staging baseline' : 'Staging repairs held'}</h4>
    <p>{humanize(baseline?.reason || 'staging_baseline_not_loaded')}</p>
    {baseline?.ready === true && <p>Approved {autonomyDate(baseline.approved_at)}. Every repair still checks the current environment and product revisions.</p>}
    {!connected && <p>Verify both the production read connection and protected staging write connection before capturing their environments.</p>}
    <div className="autonomy-actions"><button type="button" className="button button-secondary" disabled={busy || !connected} onClick={onCapture}><Layers3 size={15} />Capture environment inventory</button><button type="button" className="button button-secondary" disabled={busy} onClick={onRefresh}><RefreshCw size={15} />Refresh baseline status</button></div>
    <p>Capture reads both environments. It does not copy data, create a backup, approve parity or enable writes.</p>
    {capture && <div className="autonomy-baseline-capture"><strong>Saved inventory · {autonomyDate(capture.captured_at)}</strong><dl><div><dt>Capture ID</dt><dd><code>{capture.id}</code></dd></div><div><dt>Manifest SHA-256</dt><dd><code>{capture.manifest_sha256}</code></dd></div></dl><details><summary>Captured environment details</summary><pre>{JSON.stringify({ production: capture.production, staging: capture.staging }, null, 2)}</pre></details></div>}
    {baseline?.id && <p className="autonomy-source-note">Approved capture: <code>{baseline.id}</code></p>}
    <p>Approval requires five verified receipts: production backup, staging backup, preserved unfinished staging work, functional runtime parity, and staging isolation. Missing or unknown evidence keeps repairs held. An inventory snapshot alone is insufficient.</p>
  </div>;
}

/** One status request at a time. A hidden/unmounted dashboard never keeps polling. */
export class AutonomousStatusPoller {
  private stopped = true;
  private reading = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  constructor(private read: () => Promise<void>, private visible: () => boolean, private onError: (error: unknown) => void,
    private clock = { set: (fn: () => void, ms: number) => setTimeout(fn, ms), clear: (id: ReturnType<typeof setTimeout>) => clearTimeout(id) }) {}
  start() { this.stopped = false; this.wake(); }
  wake() {
    if (this.timer !== null) { this.clock.clear(this.timer); this.timer = null; }
    if (!this.stopped && this.visible() && !this.reading) void this.poll();
  }
  stop() { this.stopped = true; if (this.timer !== null) this.clock.clear(this.timer); this.timer = null; }
  private async poll() {
    if (this.stopped || !this.visible() || this.reading) return;
    this.reading = true;
    try { await this.read(); } catch (error) { if (!this.stopped) this.onError(error); }
    finally {
      this.reading = false;
      if (!this.stopped && this.visible()) this.timer = this.clock.set(() => { this.timer = null; void this.poll(); }, POLL_MS);
    }
  }
}

export function AutonomousProductTable({ products, next = false }: { products: AutonomousProduct[]; next?: boolean }) {
  if (!products.length) return <p className="autonomy-empty">{next ? 'No products are waiting in the next window.' : 'No products are admitted to this window yet.'}</p>;
  return <div className="autonomy-table-wrap"><table><thead><tr><th>Product</th><th>Supplier</th><th>Stage / result</th><th>Staging repair</th><th>Active / elapsed</th></tr></thead><tbody>{products.map(product => {
    const verdict = autonomyProductVerdict(product);
    return <tr key={product.id || product.product_id}>
      <td><strong>{product.title || `Product ${product.product_id}`}</strong><small>WooCommerce {product.product_id}</small></td>
      <td>{product.supplier || 'Mapping unknown'}</td>
      <td><span className={`autonomy-pill ${verdict.verified ? 'autonomy-verified' : /held|attention|failed|access|required|mismatch/.test(product.status) ? 'autonomy-attention' : ''}`}>{verdict.verified ? <ShieldCheck size={12} /> : <Clock3 size={12} />}{verdict.label}</span>{product.stage && <small>{humanize(product.stage)}</small>}<small>{verdict.unknown === null ? 'Evidence coverage unknown' : `${verdict.unknown} checks need evidence or attention`}</small>{product.reason && <small className="autonomy-reason">{humanize(product.reason)}</small>}</td>
      <td>{humanize(product.repair_status || 'not_applied')}</td>
      <td><span>{autonomyDuration(product.timing?.active_seconds)} active</span><small>{autonomyDuration(product.timing?.elapsed_seconds)} elapsed</small></td>
    </tr>;
  })}</tbody></table></div>;
}

export function environmentConnectionReady(environment: 'production' | 'staging', profile: EnvironmentProfile | undefined, token: string, username: string, password: string) {
  return (!!token.trim() || profile?.configured === true) && (environment !== 'staging' || !!username === !!password);
}

export function environmentConnectionRequest(environment: 'production' | 'staging', profile: EnvironmentProfile | undefined, token: string, username: string, password: string): EnvironmentInput {
  if (!environmentConnectionReady(environment, profile, token, username, password)) throw new Error('connector_token_or_complete_preview_credentials_required');
  const stage = environment === 'staging';
  return { base_url: profile?.base_url || (stage ? EMPTY_CONFIG.staging_url : EMPTY_CONFIG.site_url),
    role: stage ? 'write' : 'read', ...(token.trim() ? { token: token.trim() } : {}),
    ...(stage && username && password ? { protection_auth: { username, password } } : {}) };
}

export function environmentVerificationFeedback(environment: 'production' | 'staging', result: EnvironmentProfile) {
  const name = stageName(environment);
  if (result.connected === true) return { verified: true, message: `${name} connection verified. Credentials are stored privately and the input fields have been cleared.` };
  if (result.configured !== true) return { verified: false, message: `${name} connection is not verified. No saved connection was confirmed. Retry verification or review the backend error.` };
  return { verified: false, message: `Credentials saved; ${name.toLowerCase()} connection is NOT verified. ${humanize(result.reason || 'connection_verification_required')}. Leave saved credential fields blank to check again without retyping them.${environment === 'staging' ? ' If preview sign-in needs correction, enter Preview username and Preview password together; keep the saved connector token blank.' : ' Use Replace saved connector token only when changing the key.'}` };
}

export function ConnectorTokenField({ environment, token, configured, busy, onChange }: { environment: 'production' | 'staging'; token: string; configured: boolean; busy: boolean; onChange: (value: string) => void }) {
  return <label>{environment === 'staging' ? 'Staging connector token' : 'Production read token'}<input type="password" name={`hermes-${environment}-connector-token`} value={token} onChange={event => onChange(event.target.value)} autoComplete="new-password" spellCheck={false} required={!configured} placeholder={configured ? 'Saved token · leave blank to keep' : undefined} disabled={busy} /><small>Use the Hermes connector key here. Staging preview sign-in belongs in Preview username and Preview password.</small></label>;
}

export function EnvironmentConnection({ environment, profile, busy, onSave }: { environment: 'production' | 'staging'; profile?: EnvironmentProfile; busy: boolean; onSave: (environment: 'production' | 'staging', value: EnvironmentInput) => Promise<void> }) {
  const [token, setToken] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [replaceToken, setReplaceToken] = useState(false);
  const stage = environment === 'staging';
  const previewNeeded = stage && profile?.connected !== true && /401/.test(profile?.reason || '');
  const [previewOpen, setPreviewOpen] = useState(previewNeeded);
  useEffect(() => { if (previewNeeded) setPreviewOpen(true); }, [previewNeeded]);
  const base = profile?.base_url || (stage ? EMPTY_CONFIG.staging_url : EMPTY_CONFIG.site_url);
  const submit = async () => {
    try { await onSave(environment, environmentConnectionRequest(environment, profile, token, username, password)); }
    finally { setToken(''); setUsername(''); setPassword(''); }
  };
  return <form className="autonomy-environment-form" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h4>{stage ? 'Staging · supported repair writes' : 'Production · read only'}</h4>
    <p>{base}</p><span className="autonomy-pill">{profile?.connected === true ? 'Connection verified' : profile?.configured ? 'Configured · needs connection check' : 'Not connected'}</span>
    {profile?.reason && <p className="autonomy-reason">{humanize(profile.reason)}</p>}
    {profile?.configured ? <><small>Leave blank to keep the saved connector token. To check the existing connection, leave all credentials blank and select Verify.</small><details open={replaceToken} onToggle={event => { const open = event.currentTarget.open; setReplaceToken(open); if (!open) setToken(''); }}><summary>Replace saved connector token</summary>{replaceToken && <ConnectorTokenField environment={environment} token={token} configured busy={busy} onChange={setToken} />}</details></> : <ConnectorTokenField environment={environment} token={token} configured={false} busy={busy} onChange={setToken} />}
    {stage && <details open={previewOpen} onToggle={event => setPreviewOpen(event.currentTarget.open)}><summary>Staging preview protection</summary><p>Use its normal preview credentials when required. Protection stays enabled.{profile?.protected_preview_credentials_present ? ' Preview credentials are saved. Leave both blank to keep them, or enter both to replace them.' : ' Enter both username and password if the staging preview requires sign-in.'}</p><label>Preview username<input type="text" name="hermes-staging-preview-username" value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" spellCheck={false} disabled={busy} /></label><label>Preview password<input type="password" name="hermes-staging-preview-password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="new-password" disabled={busy} /></label></details>}
    <button type="submit" className="button button-secondary" disabled={busy || !environmentConnectionReady(environment, profile, token, username, password)}><ShieldCheck size={15} />{stage ? 'Verify staging write connection' : 'Verify production read connection'}</button>
    <small>Credentials stay in the protected backend and are cleared from these fields after saving. They are never included in the AI review.</small>
  </form>;
}

export default function AutonomousCompanion() {
  const [status, setStatus] = useState<AutonomyStatus | null>(null);
  const [cycles, setCycles] = useState<AutonomousCycle[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [historical, setHistorical] = useState<AutonomousCycle | null>(null);
  const [resultsPage, setResultsPage] = useState(0);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [environments, setEnvironments] = useState<EnvironmentProfiles | null>(null);
  const [directorCheck, setDirectorCheck] = useState<DirectorReadiness | null>(null);
  const [baseline, setBaseline] = useState<BaselineStatus | null>(null);
  const [baselineCapture, setBaselineCapture] = useState<BaselineCapture | null>(null);
  const [maintenanceAcknowledged, setMaintenanceAcknowledged] = useState(false);
  const [draft, setDraft] = useState<AutonomyConfig>(EMPTY_CONFIG);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const dirty = useRef(false);
  const alive = useRef(true);
  const readSerial = useRef(0);
  const providerReadSerial = useRef(0);
  const acting = useRef(false);
  const loadStatus = useCallback(async () => {
    const serial = ++readSerial.current;
    const value = await api<AutonomyStatus>(`${PREFIX}/status`);
    if (!alive.current || serial !== readSerial.current) return;
    setStatus(value);
    if (!dirty.current && value.config) setDraft({ ...EMPTY_CONFIG, ...value.config });
    setSelectedId(previous => previous || value.active_cycle_id || value.cycle?.id || '');
  }, []);
  const loadHistory = useCallback(async () => {
    const value = await api<{ items: AutonomousCycle[] }>(`${PREFIX}/cycles`);
    if (alive.current) setCycles(value.items || []);
  }, []);
  const loadEnvironments = useCallback(async () => {
    const value = await api<EnvironmentProfiles>(`${PREFIX}/environments`);
    if (alive.current) setEnvironments(value);
  }, []);
  const loadBaseline = useCallback(async () => {
    const value = await api<BaselineStatus>(`${PREFIX}/baseline`);
    if (alive.current) setBaseline(value);
  }, []);
  const loadProviders = useCallback(async () => {
    const serial = ++providerReadSerial.current;
    const value = await api<{ items: Provider[] }>('/providers');
    if (alive.current && serial === providerReadSerial.current) setProviders(value.items || []);
  }, []);
  useEffect(() => {
    alive.current = true;
    const poller = new AutonomousStatusPoller(loadStatus, () => document.visibilityState !== 'hidden', value => setError(errorMessage(value).slice(0, 500)));
    const visibility = () => poller.wake();
    poller.start();
    document.addEventListener('visibilitychange', visibility);
    void Promise.all([loadHistory(), loadEnvironments(), loadBaseline()]).catch(value => { if (alive.current) setError(errorMessage(value).slice(0, 500)); });
    return () => { alive.current = false; readSerial.current++; providerReadSerial.current++; poller.stop(); document.removeEventListener('visibilitychange', visibility); };
  }, [loadStatus, loadHistory, loadEnvironments, loadBaseline]);
  useEffect(() => {
    void loadProviders().catch(value => { if (alive.current) setError(errorMessage(value).slice(0, 500)); });
  }, [status?.config?.director_provider_id, loadProviders]);
  useEffect(() => { setMaintenanceAcknowledged(false); }, [status?.maintenance]);
  useEffect(() => { setDirectorCheck(null); }, [status?.config?.director_provider_id, status?.config?.director_enabled]);
  useEffect(() => {
    setHistorical(null);
    setResultsPage(0);
    if (!selectedId || selectedId === status?.cycle?.id) return;
    let active = true;
    void api<AutonomousCycle>(`${PREFIX}/cycles/${encodeURIComponent(selectedId)}`).then(value => { if (active) setHistorical(value); }).catch(value => { if (active) setError(errorMessage(value).slice(0, 500)); });
    return () => { active = false; };
  }, [selectedId, status?.cycle?.id]);
  const cycle = selectedId === status?.cycle?.id ? status.cycle : historical;
  const museProviders = providers.filter(isMuseProfile);
  const director = status?.director;
  const change = <K extends keyof AutonomyConfig>(key: K, value: AutonomyConfig[K]) => { dirty.current = true; setDraft(previous => ({ ...previous, [key]: value })); };
  const act = async (name: string, work: () => Promise<void>) => {
    if (acting.current) return;
    acting.current = true;
    setBusy(name); setError(''); setNotice('');
    try { await work(); await Promise.all([loadStatus(), loadHistory()]); }
    catch (value) { setError(errorMessage(value).slice(0, 500)); }
    finally { acting.current = false; setBusy(''); }
  };
  const save = () => void act('save', async () => {
    await api(`${PREFIX}/config`, { method: 'PUT', body: JSON.stringify({
      enabled: draft.enabled, schedule_enabled: draft.schedule_enabled, worker_capacity: draft.worker_capacity,
      director_enabled: draft.director_enabled, director_provider_id: draft.director_provider_id,
    }) });
    dirty.current = false; setNotice('Autonomous audit settings saved. Production changes still require your release approval.');
  });
  const create = (standardPrint = false) => void act(standardPrint ? 'pilot' : 'create', async () => {
    const value = await api<AutonomousCycle>(`${PREFIX}/cycles`, { method: 'POST', body: JSON.stringify({ kind: 'manual', ...(standardPrint ? { category_slugs: ['standard-print'] } : {}) }) });
    setSelectedId(value.id); setHistorical(value); setNotice('Audit cycle created. Inventory its products before starting workers.');
  });
  const operate = (action: 'inventory' | 'start' | 'pause' | 'resume') => void act(action, async () => {
    if (!cycle) return;
    const value = await api<AutonomousCycle>(`${PREFIX}/cycles/${encodeURIComponent(cycle.id)}/${action}`, { method: 'POST', body: '{}' });
    setHistorical(value);
    setNotice(action === 'inventory' ? 'Catalog inventory saved. Review completeness and environment checks before starting.' : action === 'pause' ? 'Pause requested. Saved work is retained; an uncertain source request remains held.' : 'Audit coordinator request accepted. Check the saved stages and blockers below.');
  });
  const refresh = () => void act('refresh', async () => {
    await Promise.all([loadProviders(), loadEnvironments(), loadBaseline()]);
    if (selectedId && selectedId !== status?.cycle?.id) setHistorical(await api<AutonomousCycle>(`${PREFIX}/cycles/${encodeURIComponent(selectedId)}`));
  });
  const loadResults = () => void act('results', async () => { if (selectedId) setHistorical(await api<AutonomousCycle>(`${PREFIX}/cycles/${encodeURIComponent(selectedId)}`)); });
  const saveEnvironment = (environment: 'production' | 'staging', value: EnvironmentInput) => act(`environment-${environment}`, async () => {
    const result = await api<EnvironmentProfile>(`${PREFIX}/environments/${environment}`, { method: 'PUT', body: JSON.stringify(value) });
    await loadEnvironments();
    const feedback = environmentVerificationFeedback(environment, result);
    if (feedback.verified) setNotice(feedback.message);
    else setError(feedback.message);
  });
  const checkDirector = () => void act('director-preflight', async () => {
    setDirectorCheck(null);
    try { setDirectorCheck(await api<DirectorReadiness>(`${PREFIX}/director/preflight`, { method: 'POST', body: '{}' })); }
    catch (value) { setDirectorCheck({ ready: false, reason: errorMessage(value).slice(0, 500) }); throw value; }
    setNotice('Director readiness checked. This does not start a supplier audit or approve a release.');
  });
  const captureBaseline = () => void act('baseline-capture', async () => {
    setBaselineCapture(await api<BaselineCapture>(`${PREFIX}/baseline/capture`, { method: 'POST', body: '{}' }));
    await loadBaseline();
    setNotice('Environment inventory captured. Backups, parity, isolation and human baseline approval are still required before staging repairs.');
  });
  const refreshBaseline = () => void act('baseline-refresh', loadBaseline);
  const recoverMaintenance = () => void act('maintenance-end', async () => {
    const request = autonomyRecoveryRequest(status?.maintenance === true, maintenanceAcknowledged);
    await api(request.path, { method: request.method, body: request.body });
    setMaintenanceAcknowledged(false);
    setNotice('Maintenance pause cleared. No cycle was resumed and no held source request was repeated.');
  });
  const activeCount = cycle?.working_window?.length;
  const nextCount = cycle?.next_window?.length;
  const hasProvider = museProviders.some(provider => provider.id === draft.director_provider_id);
  const paused = cycle?.status === 'paused';
  const scheduled = status?.enabled === true && status.schedule_enabled === true;
  const savedProducts = historical?.id === selectedId ? historical.products : undefined;
  const maintenance = status?.maintenance === true;
  const canStart = !maintenance && cycle?.inventory?.complete === true && cycle.status === 'ready';
  return <section className="panel autonomy-panel" aria-labelledby="autonomy-heading">
    <header className="autonomy-header"><div><span className="eyebrow">AUTONOMOUS COMPANION</span><h2 id="autonomy-heading">Monthly checks, with an AI director on call.</h2><p>Hermes collects and compares products, refills its queue and tests supported repairs on staging. Codex handles new code; you approve every release to main.</p></div><button className="button button-secondary" onClick={refresh} disabled={!!busy}>{busy === 'refresh' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Refresh</button></header>
    {error && <p className="autonomy-message autonomy-error" role="alert"><TriangleAlert size={16} />{error}</p>}
    {notice && <p className="autonomy-message" role="status">{notice}</p>}
    <AutonomyMaintenanceNotice maintenance={maintenance} acknowledged={maintenanceAcknowledged} busy={!!busy} onAcknowledge={setMaintenanceAcknowledged} onRecover={recoverMaintenance} />
    {!status && !error && <p className="autonomy-empty"><LoaderCircle className="spin" size={15} /> Reading the audit coordinator…</p>}
    <div className="autonomy-metrics"><div><CalendarClock size={17} /><strong>{status ? scheduled ? 'Monthly schedule on' : 'Monthly schedule off' : 'Schedule unknown'}</strong><small>{scheduled && status?.next_run_at ? `Next: ${autonomyDate(status.next_run_at)}` : 'No automatic run scheduled'} · {status?.config?.timezone || 'Timezone not loaded'}</small></div><div><Layers3 size={17} /><strong>{activeCount === undefined ? '—' : count(activeCount)} / 99 in this window</strong><small>{nextCount === undefined ? '—' : count(nextCount)} queued in the next window · {status?.config?.worker_capacity ?? '—'} worker capacity</small></div><div><ShieldCheck size={17} /><strong>Live release approval required</strong><small>Routine fixes stay on staging until approved and rechecked.</small></div></div>
    <details className="autonomy-settings"><summary><Settings2 size={16} /> Schedule and AI settings{dirty.current && <small>Unsaved changes</small>}</summary><form onSubmit={event => { event.preventDefault(); save(); }}>
      <label className="autonomy-checkbox"><input type="checkbox" checked={draft.enabled} disabled={!status || !!busy} onChange={event => change('enabled', event.target.checked)} /><span>Enable monthly autonomy<small>This enables scheduling, not a live-write permission. Manual pilots are explicit; pause an active cycle with its Pause button.</small></span></label>
      <label className="autonomy-checkbox"><input type="checkbox" checked={draft.schedule_enabled} disabled={!status || !!busy} onChange={event => change('schedule_enabled', event.target.checked)} /><span>Run calendar-month audits automatically<small>Disabled by default. Missed cycles are recovered by the coordinator after service recovery.</small></span></label>
      <label>Browser worker capacity<input type="number" min="1" max="8" required value={draft.worker_capacity} disabled={!status || !!busy} onChange={event => change('worker_capacity', Number(event.target.value))} /><small>Start with four; this coordinator allows one through eight. A 99-product window is a work queue, not 99 supplier pages at full speed.</small></label>
      <label className="autonomy-checkbox"><input type="checkbox" checked={draft.director_enabled} disabled={!status || !!busy} onChange={event => change('director_enabled', event.target.checked)} /><span>Let the Muse director review summaries and exceptions<small>Normal collection and queue refills require no AI calls.</small></span></label>
      <label>Existing Muse provider<select value={draft.director_provider_id || ''} disabled={!status || !!busy} onChange={event => change('director_provider_id', event.target.value || null)}><option value="">Choose a saved Muse profile</option>{draft.director_provider_id && !hasProvider && <option value={draft.director_provider_id}>Configured profile unavailable</option>}{museProviders.map(provider => <option key={provider.id} value={provider.id}>{provider.name} · {provider.model}</option>)}</select><small>{museProviders.length ? 'Credentials stay in Sources & AI and are never sent in audit summaries.' : 'No saved Muse profile is available. Configure it in Sources & AI before enabling AI review.'}</small></label>
      <button className="button button-primary" type="submit" disabled={!status || !!busy || (draft.director_enabled && !hasProvider)}>{busy === 'save' ? <LoaderCircle className="spin" size={15} /> : <Settings2 size={15} />}Save audit settings</button>
    </form></details>
    <div className="autonomy-budget" aria-label="AI director monthly budget"><div><strong>Muse director · {director?.month || 'month not loaded'}</strong><small>{director?.ready === true ? 'Ready for bounded summary reviews' : humanize(director?.reason || 'not_ready')}</small></div><dl>{[['Monthly cap', director?.cap_usd ?? status?.config?.director_budget_usd], ['Spent', director?.spent_usd], ['Reserved', director?.reserved_usd], ['Remaining', director?.remaining_usd]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{autonomyMoney(value)}</dd></div>)}</dl><p>Hard cap: $25 per month. At the cap, Hermes keeps collecting; additional AI reviews wait. AI cannot certify missing evidence or approve a live release.</p><button className="button button-secondary" onClick={checkDirector} disabled={!!busy || !status?.config?.director_provider_id}>{busy === 'director-preflight' ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}Check director readiness</button><DirectorReadinessDetails saved={director} checked={directorCheck} busy={busy === 'director-preflight'} /></div>
    <details className="autonomy-environments"><summary><ShieldCheck size={16} /> Environment baseline and release readiness</summary><p>Staging must have a reviewed production baseline, its intended pending changes, authenticated automation access and working rate checks. A matching version label alone is not a completed baseline.</p><div className="autonomy-environment-grid"><EnvironmentConnection environment="production" profile={environments?.production} busy={!!busy} onSave={saveEnvironment} /><EnvironmentConnection environment="staging" profile={environments?.staging} busy={!!busy} onSave={saveEnvironment} /></div><AutonomyBaselineDetails baseline={status?.baseline || baseline} capture={baselineCapture} connected={environments?.production?.connected === true && environments?.staging?.connected === true} busy={!!busy} onCapture={captureBaseline} onRefresh={refreshBaseline} />{cycle?.baseline && <details className="autonomy-cycle-context"><summary>Baseline recorded when this cycle was created</summary><pre>{JSON.stringify(cycle.baseline, null, 2)}</pre></details>}{environments?.production_release && <pre>{JSON.stringify(environments.production_release, null, 2)}</pre>}<p>Only the approved product and file differences are promoted. Live orders, customers and transactions are not replaced with the staging database.</p></details>
    <div className="autonomy-cycle-header"><div><h3>Audit cycles</h3><p>A finished collection step is not a verified product. Unknown checks remain visible.</p></div><div className="autonomy-actions"><button className="button button-primary" onClick={() => create(true)} disabled={!status || !!busy}>{busy === 'pilot' ? <LoaderCircle className="spin" size={15} /> : <CalendarClock size={15} />}Create Standard Print pilot</button><button className="button button-secondary" onClick={() => create(false)} disabled={!status || !!busy}>{busy === 'create' ? <LoaderCircle className="spin" size={15} /> : <CalendarClock size={15} />}Create full-catalog manual cycle</button></div></div>
    {cycles.length > 0 && <label className="autonomy-cycle-select">Cycle<select value={selectedId} onChange={event => setSelectedId(event.target.value)} disabled={!!busy}><option value="">Choose a cycle</option>{cycles.map(item => <option key={item.id} value={item.id}>{item.scheduled_month || autonomyDate(item.created_at)} · {humanize(item.status)} · {item.id.slice(0, 8)}</option>)}</select></label>}
    {cycle ? <><div className="autonomy-cycle-state"><strong>{humanize(cycle.status)}</strong><span>Inventory: {cycle.inventory?.complete === true ? `${count(cycle.inventory.count || 0)} products · complete` : `${humanize(cycle.inventory?.status || 'unknown')} · not complete`}</span><code>{cycle.id}</code></div><div className="autonomy-actions"><button className="button button-secondary" onClick={() => operate('inventory')} disabled={!!busy || maintenance || !['created', 'inventory_failed'].includes(cycle.status)}>{busy === 'inventory' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Read catalog inventory</button>{paused ? <button className="button button-primary" onClick={() => operate('resume')} disabled={!!busy || maintenance}><Play size={15} />Resume saved work</button> : cycle.status === 'running' ? <button className="button button-secondary" onClick={() => operate('pause')} disabled={!!busy}><Pause size={15} />Pause after checkpoint</button> : <button className="button button-primary" onClick={() => operate('start')} disabled={!!busy || !canStart}><Play size={15} />Start this cycle</button>}<button className="button button-secondary" onClick={loadResults} disabled={!!busy}>{busy === 'results' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Load saved product results</button></div>
      <div className="autonomy-counts">{['queued', 'running', 'verified', 'mismatch', 'needs_attention', 'access_required', 'failed', 'held'].map(key => <div key={key}><strong>{cycle.summary?.[key] === undefined ? '—' : count(cycle.summary[key])}</strong><small>{humanize(key)}</small></div>)}</div>
      <h4>Current window · up to 99 products</h4><AutonomousProductTable products={cycle.working_window || []} /><details className="autonomy-next"><summary>Next window · {count(cycle.next_window?.length || 0)} products</summary><AutonomousProductTable products={cycle.next_window || []} next /></details>
      {savedProducts && <details className="autonomy-next" open><summary>Saved product results · {count(savedProducts.length)}</summary><p className="autonomy-source-note">Saved snapshot. Use Load saved product results to refresh completed, held and unknown checks.</p><AutonomousProductTable products={savedProducts.slice(resultsPage * 99, (resultsPage + 1) * 99)} /><div className="autonomy-actions"><button className="button button-secondary" disabled={resultsPage === 0} onClick={() => setResultsPage(previous => previous - 1)}>Previous results</button><span>Page {resultsPage + 1} of {Math.max(1, Math.ceil(savedProducts.length / 99))}</span><button className="button button-secondary" disabled={(resultsPage + 1) * 99 >= savedProducts.length} onClick={() => setResultsPage(previous => previous + 1)}>Next results</button></div></details>}
      {cycle.supplier_blocks && Object.keys(cycle.supplier_blocks).length > 0 && <details className="autonomy-blocks" open><summary><TriangleAlert size={15} /> Supplier access or collection holds</summary><pre>{JSON.stringify(cycle.supplier_blocks, null, 2)}</pre><p>Sign in through Hermes’s own supplier browser when access is required. Other eligible suppliers continue.</p></details>}
    </> : <p className="autonomy-empty"><CircleHelp size={16} /> No audit cycle is selected. Create a manual pilot after the environment baseline is ready.</p>}
    <AutonomousRepairs releaseConnection={environments?.production_release} />
  </section>;
}

function stageName(environment: 'production' | 'staging') { return environment === 'production' ? 'Production read' : 'Staging write'; }
