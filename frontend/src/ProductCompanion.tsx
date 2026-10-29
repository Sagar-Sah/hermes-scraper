import { useCallback, useEffect, useState } from 'react';
import { ArrowRight, CheckCircle2, Clock3, Download, ExternalLink, LoaderCircle, RefreshCw, ShieldCheck, TriangleAlert, Undo2, Link2 } from 'lucide-react';
import { api, count, dateTime, downloadExport, errorMessage, getToken, humanize } from './api';
import { evidenceMoney, evidenceSelection, evidenceDisplayed, evidenceExpectedResult, evidenceCaseResult, type RenderedEvidence } from './companionEvidenceDisplay';
import './ProductCompanion.css';
import AutonomousCompanion from './AutonomousCompanion';

type Check = 'match' | 'mismatch' | 'unknown' | 'verified' | string;
interface Pair {
  id: string; product_id: string; title: string; source_url: string; storefront_url: string;
  mapping_status: string; latest_audit_id: string | null; schedule_hours: number | null;
  next_run_at: number | null; auto_correct: boolean; last_error?: string;
  pending_source_snapshot_id?: string | null; fresh_source_snapshot_id?: string | null;
  fresh_source_observed_at?: number | null; fresh_source_row_count?: number | null;
  last_source_refresh_error?: string | null;
}
interface Audit {
  id: string; pair_id: string; product_id: string; title: string; observed_at: number;
  status: string; source_url: string; storefront_url: string;
  report: {
    status: string; checks: Record<string, Check>;
    counts: { source_rows: number; site_rows: number; matching_rows: number; missing_site_rows: number; extra_site_rows: number; cost_mismatches: number };
    missing_site_rows: Record<string, string>[]; extra_site_rows: Record<string, string>[];
    cost_mismatches: { selection: Record<string, string>; supplier_cost: string; site_cost: string }[];
    source_freshness: string; retail_rule: { status: string; kind?: string; reason?: string; script_sha256?: string };
    selling_scope?: { quantity_maximum: number; authority: string } | null;
    field_checks?: Record<string, { status: string; reason: string; scope?: string; findings: FieldFinding[]; inactive_observations: { dom_ref: string; reason: string; purpose: string }[] }>;
    retail_examples: { selection: Record<string, string>; site_cost: string; expected_retail: string }[];
    proposed_changes: { kind: string; count?: number; requires_review: boolean; urls?: string[]; purpose?: string; source_label?: string }[];
    observed_source_controls?: { status: string; controls: { purpose: string; status: string; source_label: string; source_choices: { label: string; value: string }[]; storefront_dom_references: string[]; reason: string }[] };
    rendered_retail_samples?: { status: string; scope?: string; samples: RenderedEvidence[] };
  };
  evidence: Record<string, string | null>;
  review: { status: string; approved_proposal_indexes: number[] };
  read_assessment?: { reason: string; note: string; source_evidence_changed: boolean };
}
interface FieldFinding {
  id: string; kind: string; status: string; purpose: string; reason: string;
  purchase_scope: { form_key: string; scopes: string[] };
  observations: { dom_ref: string; name: string; label: string; binding_key: string; visible: boolean | null; enabled: boolean; purpose_basis: string; product_owner?: { product_id: string; verified: boolean; primary: boolean; basis: string; sources: string[] } }[];
  evidence: { url: string; projection_sha256: string; observed_at: string; observation_kind: string; viewport?: { width: number; height: number } };
  proposed_correction: { instruction: string; requires_review: boolean; automatic: boolean };
}
interface Candidate { product_id: string; title: string; storefront_url: string; mapping_status: string; supplier_url: string | null; option_audit?: { status: string; matching_paths: number; source_paths: number; differences: number; production: string } }
interface CandidateAudit {
  product_id: string; status: string; supplier_pair_status: string; source_url: string; storefront_url: string;
  observed_at?: number; site_row_count: number; site_quantities: number[]; source_quantity_domain: string;
  source_current_prices: string; source_price_requests?: number; next_step?: string;
  normalization_review?: { normalization_scope?: string; remaining_material_label_differences?: number };
  selling_scope?: { quantity_maximum: number; authority: string } | null;
  option_path_comparison?: { status: string; fields: string[]; source_option_path_count: number; site_option_path_count: number; missing_storefront_option_paths: Record<string, string>[]; extra_storefront_option_paths: Record<string, string>[] };
  source_option_contract?: { rows: { child_id: string; fields: { label: string; choice: string }[] }[] };
}
interface History { id: string; observed_at: number; status: string; counts: Audit['report']['counts'] }
interface Connector { configured: boolean; base_url?: string; connected?: boolean; reason?: string }
interface Change {
  id: string; product_id: string; pair_id: string; status: string; diff: Record<string, unknown>[];
  expected_revision: string; remote_preview_id?: string; created_at: number;
  recheck?: unknown; rollback_recheck?: unknown; error?: string; rollback_error?: string; can_rollback?: boolean; can_resume?: boolean;
}

function CandidateFindings({ result }: { result: CandidateAudit }) {
  const comparison = result.option_path_comparison;
  const missing = comparison?.missing_storefront_option_paths || [];
  const extra = comparison?.extra_storefront_option_paths || [];
  const fields = comparison?.fields || [];
  const grouped = new Map<string, { field: string; source: string; storefront: string; paths: number }>();
  // Align labels only when all other source option fields match exactly. This
  // displays a difference and never assumes the two labels are equivalent.
  for (const field of fields) {
    const context = (row: Record<string, string>) => JSON.stringify(fields.filter(key => key !== field).map(key => row[key]));
    const byContext = new Map<string, Record<string, string>[]>();
    for (const row of extra) { const key = context(row); byContext.set(key, [...(byContext.get(key) || []), row]); }
    const sourceCounts = new Map<string, number>();
    for (const row of missing) { const key = context(row); sourceCounts.set(key, (sourceCounts.get(key) || 0) + 1); }
    for (const source of missing) {
      const alternatives = byContext.get(context(source));
      if (alternatives?.length !== 1 || sourceCounts.get(context(source)) !== 1) continue;
      const site = alternatives[0];
      if (source[field] === site[field]) continue;
      const key = JSON.stringify([field, source[field], site[field]]);
      const previous = grouped.get(key);
      grouped.set(key, { field, source: source[field], storefront: site[field], paths: (previous?.paths || 0) + 1 });
    }
  }
  const rawPaths = (rows: Record<string, string>[], label: string) => <details className="companion-details"><summary>{label} · {rows.length}</summary><div className="companion-table-wrap"><table><thead><tr>{fields.map(field => <th key={field}>{humanize(field)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{fields.map(field => <td key={field}>{row[field] || '—'}</td>)}</tr>)}</tbody></table></div></details>;
  return <details className="companion-candidate-findings" open><summary>Standard Business Cards · saved source option check</summary>
    <div className="companion-candidate-findings-head"><Badge value={comparison?.status || 'unknown'} /><a href={result.source_url} target="_blank" rel="noreferrer">Exact 4over source <ExternalLink size={12} /></a>{result.observed_at && <small>Checked {dateTime(new Date(result.observed_at * 1000).toISOString())}</small>}</div>
    <p>{result.supplier_pair_status.startsWith('confirmed') ? 'The source option mapping is confirmed by the importer record and source bindings.' : 'The supplier mapping still needs review.'} This check compares option paths; current supplier quantities, prices, and production remain unverified.</p>
    <div className="companion-metrics"><div><strong>{count(result.site_row_count)}</strong><span>Stored storefront price rows</span></div><div><strong>{count(comparison?.source_option_path_count || 0)}</strong><span>Source option paths</span></div><div><strong>{count((comparison?.source_option_path_count || 0) - missing.length)}</strong><span>Matching option paths</span></div><div><strong>{count(result.normalization_review?.remaining_material_label_differences ?? Math.max(missing.length, extra.length))}</strong><span>Option paths needing review</span></div></div>
    <div className="companion-checks"><div><span>Current supplier quantities</span><Badge value={result.source_quantity_domain || 'unknown'} /></div><div><span>Current supplier prices</span><Badge value={result.source_current_prices || 'unknown'} /></div><div><span>Production</span><Badge value="unknown" /></div></div>
    <p className="companion-source-note">Storefront quantity choices: {result.site_quantities?.map(value => count(value)).join(', ') || 'Unavailable'}. Source price requests in this option check: {result.source_price_requests ?? 0}.</p>
    {result.selling_scope && <p className="companion-source-note">Merchant online quantity limit: {count(result.selling_scope.quantity_maximum)}. Hermes retains larger supplier tiers as evidence; they are outside this product’s online selling scope.</p>}
    {grouped.size > 0 && <div className="companion-table-wrap"><table><thead><tr><th>Field</th><th>4over label</th><th>Storefront label</th><th>Paths</th></tr></thead><tbody>{[...grouped.values()].map((entry, index) => <tr key={index}><td>{humanize(entry.field)}</td><td>{entry.source}</td><td>{entry.storefront}</td><td>{entry.paths}</td></tr>)}</tbody></table></div>}
    {result.normalization_review?.normalization_scope && <p className="companion-source-note">{result.normalization_review.normalization_scope}</p>}
    {rawPaths(missing, 'All source paths missing from the storefront labels')}{rawPaths(extra, 'All storefront paths differing from source labels')}
    {result.source_option_contract?.rows && <details className="companion-details"><summary>All raw source option rows · {result.source_option_contract.rows.length}</summary><div className="companion-table-wrap"><table><thead><tr><th>4over child</th><th>Exact source choices</th></tr></thead><tbody>{result.source_option_contract.rows.map(row => <tr key={row.child_id}><td>{row.child_id}</td><td>{row.fields.map(field => `${field.label}: ${field.choice}`).join(' · ')}</td></tr>)}</tbody></table></div></details>}
  </details>;
}

function ChangeDetails({ change }: { change: Change }) {
  return <><div className="companion-change-meta"><strong>{humanize(change.status)}</strong><span>{dateTime(new Date(change.created_at * 1000).toISOString())}</span></div>
    <p className="companion-revision">Expected revision <code>{change.expected_revision}</code></p>
    <div className="companion-change-diff">{change.diff?.map((entry, index) => <details key={index} open><summary>{humanize(String(entry.kind || entry.field || `Change ${index + 1}`))}</summary><pre>{JSON.stringify(entry, null, 2)}</pre></details>)}</div>
    {change.error && <p className="companion-write-error" role="alert">{change.error}</p>}
    {change.status === 'applying' && change.can_resume && <p className="companion-source-note">The remote write may have completed. Recover its saved status before taking another action.</p>}
    {change.status === 'rollback_recheck_pending' && <p className="companion-source-note">The saved version has been restored. Only the storefront recheck remains.</p>}
    {change.rollback_error && <p className="companion-write-error" role="alert">Rollback check: {change.rollback_error}</p>}
    {change.recheck !== undefined && <details className="companion-details"><summary>Storefront recheck</summary><pre className="companion-write-json">{JSON.stringify(change.recheck, null, 2)}</pre></details>}
    {change.rollback_recheck !== undefined && <details className="companion-details"><summary>Restored storefront recheck</summary><pre className="companion-write-json">{JSON.stringify(change.rollback_recheck, null, 2)}</pre></details>}
  </>;
}

function Badge({ value }: { value: string }) {
  return <span className={`companion-badge companion-${value}`}>{value === 'match' || value === 'verified' || value === 'green' ? <CheckCircle2 size={13} /> : value === 'unknown' ? <Clock3 size={13} /> : <TriangleAlert size={13} />}{humanize(value)}</span>;
}

export default function ProductCompanion() {
  const [pairs, setPairs] = useState<Pair[]>([]);
  const [selected, setSelected] = useState('');
  const [audit, setAudit] = useState<Audit | null>(null);
  const [history, setHistory] = useState<History[]>([]);
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [candidateAudit, setCandidateAudit] = useState<CandidateAudit | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [chosenProposals, setChosenProposals] = useState<number[]>([]);
  const [connector, setConnector] = useState<Connector | null>(null);
  const [connectorUrl, setConnectorUrl] = useState('');
  const [connectorToken, setConnectorToken] = useState('');
  const [changes, setChanges] = useState<Change[]>([]);
  const [writePreview, setWritePreview] = useState<Change | null>(null);

  const loadPairs = useCallback(async () => {
    const data = await api<{ items: Pair[] }>('/companion/pairs');
    setPairs(data.items);
    setSelected(previous => data.items.some(pair => pair.id === previous) ? previous : data.items[0]?.id || '');
  }, []);
  useEffect(() => { void loadPairs().catch(e => setError(errorMessage(e))); }, [loadPairs]);
  const loadConnector = useCallback(async () => {
    const value = await api<Connector>('/companion/connector');
    setConnector(value); setConnectorUrl(value.base_url || '');
  }, []);
  useEffect(() => { void loadConnector().catch(e => setError(errorMessage(e))); }, [loadConnector]);
  const loadChanges = useCallback(async (pairId: string) => {
    const value = await api<{ items: Change[] }>(`/companion/pairs/${pairId}/changes`);
    setChanges(value.items);
  }, []);
  useEffect(() => {
    setWritePreview(null); setChosenProposals([]); setChanges([]);
    if (!selected) return;
    let active = true;
    void api<{ items: Change[] }>(`/companion/pairs/${selected}/changes`).then(value => { if (active) setChanges(value.items); }).catch(e => { if (active) setError(errorMessage(e)); });
    return () => { active = false; };
  }, [selected]);
  useEffect(() => { setChosenProposals([]); setWritePreview(null); }, [audit?.id]);
  useEffect(() => {
    if (!selected) { setAudit(null); setHistory([]); return; }
    let active = true;
    void Promise.all([
      api<{ items: History[] }>(`/companion/pairs/${selected}/audits`),
      pairs.find(pair => pair.id === selected)?.latest_audit_id
        ? api<Audit>(`/companion/audits/${pairs.find(pair => pair.id === selected)!.latest_audit_id}`)
        : Promise.resolve(null),
    ]).then(([h, a]) => { if (active) { setHistory(h.items); setAudit(a); } }).catch(e => { if (active) setError(errorMessage(e)); });
    return () => { active = false; };
  }, [selected, pairs]);

  const act = async (label: string, work: () => Promise<void>) => {
    setBusy(label); setError(''); setNotice('');
    try { await work(); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  };
  const importPairs = () => void act('import', async () => {
    const result = await api<{ added: string[]; reason?: string }>('/companion/import-confirmed-business-cards', { method: 'POST' });
    await loadPairs();
    setNotice(result.added.length ? `${result.added.length} confirmed 4over business-card pair added.` : result.reason ? 'No confirmed business-card campaign is loaded in this Hermes workspace.' : 'Confirmed mappings are already present.');
  });
  const loadStandardCandidate = async () => {
    const token = getToken();
    const response = await fetch('/api/v1/companion/candidates/6862', { signal: AbortSignal.timeout(12000), headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (response.status === 404) { setCandidateAudit(null); return; }
    if (!response.ok) throw new Error(`The saved Standard Business Cards check could not be loaded (${response.status}).`);
    setCandidateAudit(await response.json() as CandidateAudit);
  };
  const discover = () => void act('discover', async () => {
    const result = await api<{ items: Candidate[] }>('/companion/business-card-candidates');
    setCandidates(result.items);
    await loadStandardCandidate();
  });
  const checkCandidate = (item: Candidate) => void act(`candidate-${item.product_id}`, async () => {
    if (item.product_id !== '6862' || !item.supplier_url) return;
    const result = await api<CandidateAudit>('/companion/candidates/6862/discover', { method: 'POST', body: JSON.stringify({ source_url: item.supplier_url }) });
    setCandidateAudit(result);
    const inventory = await api<{ items: Candidate[] }>('/companion/business-card-candidates');
    setCandidates(inventory.items);
    setNotice('Standard Business Cards source options checked and saved. Current quantity tiers and supplier prices still require separate verification.');
  });
  const run = () => void act('audit', async () => {
    const result = await api<Audit>(`/companion/pairs/${selected}/audits`, { method: 'POST' });
    setAudit(result); setChosenProposals([]);
    await loadPairs();
    setNotice('Audit saved. Unknown checks require more current evidence before completion.');
  });
  const refreshChildTables = () => void act('refresh-source', async () => {
    const result = await api<{ child_count: number; row_count: number }>(`/companion/pairs/${selected}/refresh-child-tables`, { method: 'POST' });
    await loadPairs();
    setNotice(`Saved ${result.row_count} current child-table rows across ${result.child_count} 4over options. Run audit to compare them; service fees and payable totals still require separate verification.`);
  });
  const saveSchedule = (hours: number | null) => void act('schedule', async () => {
    await api(`/companion/pairs/${selected}/schedule`, { method: 'PUT', body: JSON.stringify({ hours, auto_correct: false }) });
    await loadPairs();
    setNotice(hours ? `Recurring read-only audit set for every ${hours} hours.` : 'Recurring audit turned off.');
  });
  const approve = () => void act('review', async () => {
    if (!audit) return;
    const result = await api<Audit>(`/companion/audits/${audit.id}/review`, { method: 'POST', body: JSON.stringify({ approved_proposal_indexes: chosenProposals }) });
    setAudit(result);
    setNotice('Proposal review recorded. No storefront change has been made.');
  });
  const exportAudit = () => void act('export', async () => {
    if (!audit) return;
    await downloadExport({ name: `hermes-companion-${audit.product_id}-${audit.id}.zip`, url: `/api/v1/companion/audits/${audit.id}/export` });
  });
  const saveConnector = () => void act('connector', async () => {
    try {
      const result = await api<Connector>('/companion/connector', { method: 'PUT', body: JSON.stringify({ base_url: connectorUrl.trim(), token: connectorToken }) });
      setConnector(result); setConnectorUrl(result.base_url || connectorUrl.trim());
      setNotice(result.connected ? 'WooCommerce connector verified. Preview each proposed change before applying it.' : 'Connector token saved privately. Staging preview authentication still blocks the external API; the connection is not verified.');
    } finally { setConnectorToken(''); }
  });
  const previewWrite = () => void act('write-preview', async () => {
    if (!audit) return;
    const result = await api<Change>(`/companion/audits/${audit.id}/write-preview`, { method: 'POST', body: JSON.stringify({ proposal_indexes: chosenProposals }) });
    setWritePreview(result); await loadChanges(selected);
    setNotice('Write preview prepared. Review the exact changes and revision before applying.');
  });
  const applyWrite = (change: Change) => void act(`write-apply-${change.id}`, async () => {
    const result = await api<Change>(`/companion/changes/${change.id}/apply`, { method: 'POST', body: JSON.stringify({ expected_revision: change.expected_revision }) });
    setWritePreview(result); await loadChanges(selected);
    setNotice(result.status === 'applied_verified' ? 'Change applied and storefront recheck passed.' : result.status === 'rolled_back_after_failed_recheck' ? 'The storefront recheck failed. Hermes restored the saved version.' : result.status === 'applying' && result.can_resume ? 'The write status is uncertain. Use Recover write status to check the saved remote receipt.' : result.status === 'rollback_recheck_pending' ? 'The saved version was restored. Recheck the restored storefront to finish verification.' : 'The write needs attention. Review the saved result and recheck before continuing.');
  });
  const rollbackWrite = (change: Change) => void act(`rollback-${change.id}`, async () => {
    const result = await api<Change>(`/companion/changes/${change.id}/rollback`, { method: 'POST', body: '{}' });
    if (writePreview?.id === result.id) setWritePreview(result);
    await loadChanges(selected);
    setNotice(result.status === 'rollback_verified' ? 'Saved version restored and storefront recheck passed.' : result.status === 'rollback_recheck_pending' ? 'The saved version was restored. The storefront still needs a successful recheck.' : 'Rollback needs attention. Review the saved result before continuing.');
  });
  const pair = pairs.find(item => item.id === selected);
  const checks = audit ? Object.entries(audit.report.checks) : [];
  const changeActions = (change: Change) => <div className="companion-write-actions">
    {(change.status === 'preview' || (change.status === 'applying' && change.can_resume)) && <button className="button button-primary" onClick={() => applyWrite(change)} disabled={!!busy}>{busy === `write-apply-${change.id}` ? <LoaderCircle size={15} className="spin" /> : change.status === 'applying' ? <RefreshCw size={15} /> : <ShieldCheck size={15} />}{change.status === 'applying' ? 'Recover write status' : 'Apply this preview and recheck storefront'}</button>}
    {change.status === 'rollback_recheck_pending' && change.can_resume && <button className="button button-secondary" onClick={() => rollbackWrite(change)} disabled={!!busy}>{busy === `rollback-${change.id}` ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}Recheck restored storefront</button>}
    {change.status !== 'rollback_recheck_pending' && (change.can_rollback ?? ['applied_verified', 'applied'].includes(change.status)) && <button className="button button-secondary" onClick={() => rollbackWrite(change)} disabled={!!busy}>{busy === `rollback-${change.id}` ? <LoaderCircle size={15} className="spin" /> : <Undo2 size={15} />}Restore saved version</button>}
  </div>;

  return <div className="companion-page">
    <header className="companion-heading"><div><span className="eyebrow">PRODUCT COMPANION</span><h1>Keep your products in step with suppliers.</h1><p>Compare verified supplier choices and costs with the live storefront. Every result links back to saved evidence.</p></div><div className="companion-heading-actions"><button className="button button-secondary" onClick={discover} disabled={!!busy}>{busy === 'discover' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Inventory business cards</button><button className="button button-primary" onClick={importPairs} disabled={!!busy}>{busy === 'import' ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}Import confirmed 4over pairs</button></div></header>
    <AutonomousCompanion />
    {error && <div className="companion-message companion-error" role="alert"><TriangleAlert size={17} />{error}</div>}
    {notice && <div className="companion-message" role="status"><CheckCircle2 size={17} />{notice}</div>}
    <details className="panel companion-connector"><summary><span><Link2 size={17} />WooCommerce write connector</span><Badge value={connector?.configured && connector.connected === true ? 'verified' : connector?.configured && connector.connected === false ? 'mismatch' : 'unknown'} /></summary><p>Connect the installed Hermes WordPress plugin to prepare and apply scoped calculator changes. Each write starts with a preview and keeps a saved version for rollback.</p>
      <form onSubmit={event => { event.preventDefault(); saveConnector(); }} className="companion-connector-form">
        <label>WordPress site address<input type="url" required value={connectorUrl} onChange={e => setConnectorUrl(e.target.value)} placeholder="https://printitprints.com" autoComplete="url" disabled={!!busy} /></label>
        <label>Connector token<input type="password" required value={connectorToken} onChange={e => setConnectorToken(e.target.value)} placeholder={connector?.configured ? 'Enter token to update connection' : 'Token from the WordPress plugin'} autoComplete="off" spellCheck={false} disabled={!!busy} /></label>
        <button type="submit" className="button button-secondary" disabled={!!busy || !connectorUrl.trim() || !connectorToken.trim()}>{busy === 'connector' ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}Save connection</button>
      </form><small>The token is stored in the local backend and cleared from this form after saving. It is never included in audit exports. Automatic corrections stay off unless separately configured.</small>{connector?.reason && <p className="companion-write-error">{humanize(connector.reason)}</p>}
    </details>
    <div className="companion-grid">
      <section className="panel companion-pairs"><h2>Mapped products</h2><p>Only exact, confirmed 4over mappings enter this audit.</p>{pairs.length ? pairs.map(item => <button key={item.id} className={`companion-pair ${selected === item.id ? 'selected' : ''}`} onClick={() => setSelected(item.id)} disabled={!!busy}><strong>{item.title}</strong><small>WooCommerce {item.product_id}</small><span>{item.latest_audit_id ? 'Last audit saved' : 'Ready for first audit'} <ArrowRight size={13} /></span></button>) : <p className="companion-empty">Import confirmed business-card mappings to begin.</p>}</section>
      <section className="panel companion-detail">{pair ? <><div className="companion-detail-head"><div><span className="eyebrow">WOOCOMMERCE {pair.product_id}</span><h2>{pair.title}</h2><div className="companion-links"><a href={pair.storefront_url} target="_blank" rel="noreferrer">Storefront <ExternalLink size={12} /></a><a href={pair.source_url} target="_blank" rel="noreferrer">4over source <ExternalLink size={12} /></a></div></div><div className="companion-heading-actions"><button className="button button-secondary" onClick={refreshChildTables} disabled={!!busy}>{busy === 'refresh-source' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Refresh 4over child tables</button><button className="button button-primary" onClick={run} disabled={!!busy}>{busy === 'audit' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Run audit</button></div></div>
        {pair.fresh_source_observed_at && <p className="companion-source-note">Current child table snapshot: {pair.fresh_source_row_count} rows saved {dateTime(new Date(pair.fresh_source_observed_at * 1000).toISOString())}. This covers source tables only.</p>}
        {pair.last_source_refresh_error && <div className="companion-message companion-error" role="status"><TriangleAlert size={16} />Source refresh paused: {pair.last_source_refresh_error}. Saved child responses will be reused on retry.</div>}
        <div className="companion-schedule"><div><strong>Recurring check</strong><small>Opt in. Supplier calls are serialized and saved prices remain historical until quoted again.</small></div><select aria-label="Recurring audit interval" value={pair.schedule_hours ?? 0} disabled={!!busy} onChange={e => saveSchedule(Number(e.target.value) || null)}><option value={0}>Off</option><option value={24}>Every day</option><option value={168}>Every week</option><option value={720}>Every 30 days</option></select></div>
        {pair.last_error && <div className="companion-message companion-error" role="status"><TriangleAlert size={16} />Last scheduled check stopped: {humanize(pair.last_error)}. The previous audit is retained.</div>}
        {audit ? <><div className="companion-audit-title"><div><Badge value={audit.status} /><span>Saved {dateTime(new Date(audit.observed_at * 1000).toISOString())}</span></div><button className="button button-secondary" onClick={exportAudit} disabled={!!busy}><Download size={15} />Export evidence ZIP</button></div>
          <p className="companion-caveat">{audit.report.source_freshness === 'rendered_page_observation_only_no_current_price_table' ? 'This audit checks observed fields and declared supplier menus. Supplier prices and complete conditional option coverage remain unknown. No supplier cost table has been verified by this audit.' : audit.report.source_freshness === 'current_child_price_tables_only' ? 'Current 4over child tables cover one set with sample and digital proof off. They do not establish service fees, shipping, or a payable order total. Unknown checks prevent a green completion.' : 'Saved supplier cost rows are historical. A matching table and current child list do not establish today’s exact prices or a green completion.'}</p>
          {audit.read_assessment && <p className="companion-source-note">Saved audit reassessed: {audit.read_assessment.note} The original source evidence is unchanged; unknown checks need new observations.</p>}
          {audit.report.selling_scope && <p className="companion-source-note">Merchant online quantity limit: {count(audit.report.selling_scope.quantity_maximum)}. Larger supplier tiers remain in the evidence and are excluded from missing online selections.</p>}
          <div className="companion-metrics"><div><strong>{count(audit.report.counts.source_rows)}</strong><span>Supplier rows</span></div><div><strong>{count(audit.report.counts.site_rows)}</strong><span>Store rows</span></div><div><strong>{count(audit.report.counts.cost_mismatches)}</strong><span>Cost differences</span></div><div><strong>{count(audit.report.counts.missing_site_rows)}</strong><span>Missing selections</span></div></div>
          <h3>Checks</h3><div className="companion-checks">{checks.map(([name, value]) => <div key={name}><span>{humanize(name)}</span><Badge value={value} /></div>)}</div>
          {audit.report.field_checks && <details className="companion-details"><summary>Repeated fields and headings · review evidence</summary><p>These checks cover the observed purchase state and viewport. A similar label alone does not prove a duplicate. Hidden or disabled copies are retained as inactive observations. Hermes never removes a field automatically.</p>{Object.entries(audit.report.field_checks).map(([location, check]) => <div key={location}><h4>{humanize(location)} · {humanize(check.status)}</h4><p>{humanize(check.reason)}{check.scope ? ` · ${humanize(check.scope)}` : ''}</p>{check.findings.map(finding => <article key={finding.id}><strong>{humanize(finding.purpose)} · {humanize(finding.status)}</strong><p>{humanize(finding.reason)} · {finding.purchase_scope.form_key}{finding.purchase_scope.scopes.length ? ` · ${finding.purchase_scope.scopes.join(' · ')}` : ''}</p><div className="companion-table-wrap"><table><thead><tr><th>DOM reference</th><th>Name / label</th><th>Binding</th><th>Product owner</th><th>Visible / enabled</th></tr></thead><tbody>{finding.observations.map(field => <tr key={field.dom_ref}><td>{field.dom_ref}</td><td>{field.name || '—'} / {field.label || '—'}</td><td>{field.binding_key || 'Unverified'}</td><td>{field.product_owner?.verified ? `${field.product_owner.product_id}${field.product_owner.primary ? ' · primary' : ''}` : 'Unverified'}<small>{field.product_owner?.sources?.join(' · ')}</small></td><td>{field.visible === null ? 'Unknown' : field.visible ? 'Visible' : 'Hidden'} / {field.enabled ? 'Enabled' : 'Disabled'}</td></tr>)}</tbody></table></div><p>{finding.proposed_correction.instruction} Review required.</p><small>{humanize(finding.evidence.observation_kind)} · {finding.evidence.observed_at} · evidence {finding.evidence.projection_sha256}</small></article>)}{!!check.inactive_observations.length && <p>{count(check.inactive_observations.length)} inactive field observations retained.</p>}</div>)}</details>}
          {audit.report.retail_examples.length > 0 && <details className="companion-details"><summary>Retail calculation examples</summary><p>Calculated from the pinned site rule. These examples do not establish the full rendered price domain or cart total.</p><div className="companion-table-wrap"><table><thead><tr><th>Selection</th><th>Stored cost</th><th>Expected retail</th></tr></thead><tbody>{audit.report.retail_examples.map((item, i) => <tr key={i}><td>{Object.values(item.selection).join(' · ')}</td><td>${item.site_cost}</td><td>${item.expected_retail}</td></tr>)}</tbody></table></div></details>}
          {audit.report.observed_source_controls && <details className="companion-details"><summary>Observed supplier controls · {humanize(audit.report.observed_source_controls.status)}</summary><p>These are declared supplier controls compared with this storefront state. An absent control requires review of the intended online offer, conditional rules and price. Other states and complete source coverage remain unknown.</p><div className="companion-table-wrap"><table><thead><tr><th>Supplier control</th><th>Observed supplier choices</th><th>Current storefront evidence</th></tr></thead><tbody>{audit.report.observed_source_controls.controls.map((item, i) => <tr key={i}><td>{item.source_label}</td><td>{item.source_choices.map(option => option.label).join(' · ')}</td><td>{humanize(item.status)} · {humanize(item.reason)}{item.storefront_dom_references.length ? <small>{item.storefront_dom_references.join(' · ')}</small> : null}</td></tr>)}</tbody></table></div></details>}
          {audit.report.rendered_retail_samples && <details className="companion-details"><summary>Rendered customer price checks · {humanize(audit.report.rendered_retail_samples.status)}</summary><p>These checks cover only the exact selections shown. They do not prove the full price domain or checkout total. A dash means no verified value was supplied for that case.</p><div className="companion-table-wrap"><table><thead><tr><th>Selection</th><th>Stored cost</th><th>Expected result</th><th>Expected retail</th><th>Displayed</th><th>Case result</th></tr></thead><tbody>{audit.report.rendered_retail_samples.samples.map((item, i) => <tr key={i}><td>{evidenceSelection(item.selection)}{item.case_id && <small>{item.case_id}</small>}</td><td>{evidenceMoney(item.site_cost)}</td><td>{evidenceExpectedResult(item)}</td><td>{evidenceMoney(item.expected_retail, item.expected?.startsWith('blocked'))}</td><td>{evidenceDisplayed(item)}{item.observed?.warning_text && <small>{item.observed.warning_text}</small>}</td><td>{evidenceCaseResult(item)}</td></tr>)}</tbody></table></div></details>}
          {audit.report.missing_site_rows.length > 0 && <details className="companion-details"><summary>Missing storefront configurations · {audit.report.missing_site_rows.length}</summary><div className="companion-table-wrap"><table><thead><tr><th>Supplier selection</th></tr></thead><tbody>{audit.report.missing_site_rows.map((row, i) => <tr key={i}><td>{Object.entries(row).map(([key, value]) => `${humanize(key)}: ${value}`).join(' · ')}</td></tr>)}</tbody></table></div></details>}
          {audit.report.extra_site_rows.length > 0 && <details className="companion-details"><summary>Extra storefront configurations · {audit.report.extra_site_rows.length}</summary><div className="companion-table-wrap"><table><thead><tr><th>Storefront selection</th></tr></thead><tbody>{audit.report.extra_site_rows.map((row, i) => <tr key={i}><td>{Object.entries(row).map(([key, value]) => `${humanize(key)}: ${value}`).join(' · ')}</td></tr>)}</tbody></table></div></details>}
          {audit.report.cost_mismatches.length > 0 && <details className="companion-details"><summary>Stored supplier cost differences · {audit.report.cost_mismatches.length}</summary><div className="companion-table-wrap"><table><thead><tr><th>Selection</th><th>Current supplier table</th><th>Stored site cost</th></tr></thead><tbody>{audit.report.cost_mismatches.map((row, i) => <tr key={i}><td>{Object.values(row.selection).join(' · ')}</td><td>${row.supplier_cost}</td><td>${row.site_cost}</td></tr>)}</tbody></table></div></details>}
          {audit.report.proposed_changes.length > 0 && <div className="companion-proposals"><h3>Proposed changes</h3>{audit.report.proposed_changes.map((item, i) => <label key={i} className="companion-proposal"><input type="checkbox" checked={chosenProposals.includes(i)} disabled={!!busy} onChange={e => setChosenProposals(current => e.target.checked ? [...current, i] : current.filter(index => index !== i))} /><span>{humanize(item.kind)}{item.source_label || item.purpose ? ` · ${item.source_label || humanize(item.purpose!)}` : ''}{item.count === undefined ? '' : ` · ${count(item.count)}`} · review required{item.urls?.map(url => <small key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a></small>)}</span></label>)}<div className="companion-write-actions"><button className="button button-secondary" onClick={approve} disabled={!!busy || audit.review.status === 'approved' || chosenProposals.length === 0}>{audit.review.status === 'approved' ? 'Review recorded' : `Approve ${chosenProposals.length} selected for handoff`}</button><button className="button button-primary" onClick={previewWrite} disabled={!!busy || connector?.connected !== true || chosenProposals.length === 0}>{busy === 'write-preview' ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}Preview selected changes</button></div><small>Preview checks the current product revision and evidence. Apply is a separate action after reviewing the exact difference below.</small></div>}
          {writePreview && <section className="companion-write-preview" aria-label="Calculator write preview"><h3>Calculator change preview</h3><ChangeDetails change={writePreview} />{changeActions(writePreview)}</section>}
          <details className="companion-details"><summary>Evidence fingerprints</summary><dl>{Object.entries(audit.evidence).map(([key, value]) => <div key={key}><dt>{humanize(key)}</dt><dd>{value || 'Unavailable'}</dd></div>)}</dl></details>
        </> : <p className="companion-empty">Run the first audit to see choices, costs, content checks, and evidence.</p>}
        {history.length > 1 && <details className="companion-details"><summary>Audit history · {history.length}</summary><ul>{history.map(item => <li key={item.id}><button disabled={!!busy} onClick={() => void api<Audit>(`/companion/audits/${item.id}`).then(setAudit).catch(e => setError(errorMessage(e)))}>{dateTime(new Date(item.observed_at * 1000).toISOString())} · {humanize(item.status)} · {item.counts.cost_mismatches} cost differences</button></li>)}</ul></details>}
        {changes.length > 0 && <details className="companion-details companion-write-history"><summary>Calculator change history · {changes.length}</summary>{changes.map(change => <details className="companion-history-change" key={change.id}><summary>{dateTime(new Date(change.created_at * 1000).toISOString())} · {humanize(change.status)}</summary><ChangeDetails change={change} />{changeActions(change)}</details>)}</details>}
      </> : <p className="companion-empty">Choose a mapped product to inspect.</p>}</section>
    </div>
    {candidates && <section className="panel companion-candidates"><h2>Business-card inventory</h2><p>Supplier mappings require exact product evidence. Unresolved products stay outside the confirmed 4over audit.</p><div className="companion-table-wrap"><table><thead><tr><th>Product</th><th>Woo ID</th><th>4over mapping</th><th>Saved option check</th><th>Action</th></tr></thead><tbody>{candidates.map(item => <tr key={item.product_id}><td><a href={item.storefront_url} target="_blank" rel="noreferrer">{item.title}</a>{item.supplier_url && <small className="companion-candidate-source"><a href={item.supplier_url} target="_blank" rel="noreferrer">{item.supplier_url}</a></small>}</td><td>{item.product_id}</td><td><Badge value={item.mapping_status.startsWith('confirmed') ? 'verified' : 'unknown'} /></td><td>{item.option_audit ? <><Badge value={item.option_audit.status} /><small className="companion-candidate-source">{count(item.option_audit.matching_paths)} / {count(item.option_audit.source_paths)} paths match · {count(item.option_audit.differences)} need review</small></> : <Badge value="unknown" />}</td><td>{item.product_id === '6862' && item.supplier_url && <button className="button button-secondary" disabled={!!busy} onClick={() => checkCandidate(item)}>{busy === `candidate-${item.product_id}` ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}Check source options</button>}</td></tr>)}</tbody></table></div>{candidateAudit && <CandidateFindings result={candidateAudit} />}</section>}
  </div>;
}
