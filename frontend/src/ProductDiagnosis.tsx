import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Check, CheckCircle2, ExternalLink, ListChecks, LoaderCircle, Plus, RefreshCw, ShieldCheck, Trash2, TriangleAlert } from 'lucide-react';
import { api, post, del, errorMessage, downloadExport, count, humanize, type DiagnosisField, type DiagnosisSelection, type DiagnosisSelectionField, type ProductDiagnosisRecord, type SupplierSession, type NewSupplierSession, type Run } from './api';
import './ProductDiagnosis.css';
import DiagnosisLaunch from './DiagnosisLaunch';
import PricingPilot from './PricingPilot';

const pending = (record: ProductDiagnosisRecord | null) => !!record && ['opening', 'analyzing'].includes(record.status);
const fieldLabel = (field: DiagnosisField) => field.label?.trim() || 'Unlabeled field';
const sessionLabels: Record<SupplierSession['status'], string> = { not_open: 'Browser not opened', signed_in: 'Signed in', login_required: 'Sign-in needed', unknown: 'Check sign-in', opening: 'Opening browser', error: 'Needs attention', closed: 'Browser closed', attention: 'Needs attention' };
const diagnosisLabels: Record<ProductDiagnosisRecord['status'], string> = { opening: 'Opening product', analyzing: 'Reading current page', ready: 'Ready for field review', login_required: 'Supplier sign-in needed', attention: 'Needs review', error: 'Analysis failed', closed: 'Product page closed' };
const warningMessages: Record<string, string> = {
  supplier_price_login_required: 'The supplier hides this product’s price until you sign in. Use Open supplier browser above, sign in there, then refresh this product.',
  initial_snapshot_not_settled: 'The product options are still loading. Wait for them to settle in the Hermes browser, then refresh this diagnosis.',
  page_not_open: 'The product page is not open. Analyze its URL again to reopen it in the Hermes browser.',
  no_visible_configurable_controls: 'No configurable product controls were visible. Open the product calculator or options in the Hermes browser, then refresh.',
  selection_cleared_after_page_change: 'The page options changed. Review your fields and fixed choices before saving a new policy.',
  diagnosis_limits_reached: 'Some fields could not be included in this snapshot. The diagnosis covers only the controls Hermes could read.',
  page_timeout: 'The supplier page took too long to respond. Check it in the Hermes browser, wait for it to load, then refresh.',
  page_unavailable: 'The supplier page could not be read. Check it in the Hermes browser, then refresh or analyze the URL again.',
  unexpected_source_origin: 'The browser moved to a different site. Return to the intended supplier product page in the Hermes browser, then refresh.',
  product_page_required: 'This page is not a product page. Open the product calculator in the Hermes browser, then refresh.',
  field_label_from_choices: 'A field name was taken from its visible choices. Confirm it describes the product option you intend to keep.',
  unsupported_visible_attribute: 'Some visible product options need supplier support before this diagnosis can be used for a complete run.',
  custom_dropdown_inspection_incomplete: 'Some dropdowns were not fully inspected. Wait for the product options to settle in the Hermes browser, then refresh.',
  custom_dropdown_choices_unavailable: 'A dropdown’s choices could not be read. Open that option in the Hermes browser, then refresh.',
  custom_dropdown_labels_ambiguous: 'A dropdown has repeated choice labels that Hermes cannot distinguish safely. Review that option before saving a policy.',
  custom_dropdown_inspection_failed: 'A dropdown could not be inspected. Check the product options in the Hermes browser, then refresh.',
  custom_dropdown_close_failed: 'A dropdown stayed open after inspection. Close it in the Hermes browser, then refresh to confirm the page state.',
  custom_dropdown_selection_changed: 'A dropdown selection changed during inspection. Confirm the intended choice in the Hermes browser, then refresh before fixing that field.',
};
const warningText = (warning: string) => warningMessages[warning] || (/^[a-z0-9_]+$/.test(warning) ? humanize(warning) : warning);
function safeUrl(value: string): URL | null {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url : null; } catch { return null; }
}
const numericField = (field: DiagnosisField) => ['number', 'numeric', 'range'].includes(field.kind);
const finiteBound = (value: string | null | undefined) => value != null && value.trim() !== '' && Number.isFinite(Number(value)) ? Number(value) : undefined;
function validFixed(field: DiagnosisField, value: string) {
  if (field.choices.length) return field.choices.some(choice => choice.value === value && !choice.disabled && !choice.placeholder && !!choice.label?.trim());
  if (!value.trim()) return false;
  if (!numericField(field) || !Number.isFinite(Number(value))) return false;
  const number = Number(value), min = finiteBound(field.min), max = finiteBound(field.max);
  if ((min != null && number < min) || (max != null && number > max)) return false;
  if (field.step != null && field.step !== 'any') {
    const step = finiteBound(field.step);
    if (step == null || step <= 0) return false;
    const steps = (number - (min ?? 0)) / step;
    if (Math.abs(steps - Math.round(steps)) > 1e-8) return false;
  }
  return true;
}
const multipleField = (field: DiagnosisField) => field.multiple === true || field.kind === 'checkbox';
const needsSupplierBinding = (field: DiagnosisField) => field.binding_kind === 'display_label_only' || field.value_origin === 'display_label_surrogate' || field.kind === 'custom_select';
function placeholderFor(field: DiagnosisField) { let value = '__hermes_choose__'; while (field.choices.some(choice => choice.value === value)) value += '_'; return value; }
function validFixedSelection(field: DiagnosisField, values: string[]) {
  if (!values.length) return multipleField(field) && Array.isArray(field.selected_values) && field.selected_values.length === 0;
  return (multipleField(field) || values.length === 1) && values.every(value => validFixed(field, value)) && (!field.disabled || (field.selected_values != null && values.length === field.selected_values.length && values.every(value => field.selected_values!.includes(value))));
}
function draftFor(record: ProductDiagnosisRecord): DiagnosisSelectionField[] {
  const saved = record.selection?.revision === record.revision ? record.selection : null;
  return (record.fields || []).map(field => {
    const previous = saved?.fields.find(item => item.key === field.key);
    const fixed = previous && !previous.include ? previous.fixed_values : field.selected_values || [];
    return { key: field.key, include: previous?.include ?? true, fixed_values: field.configurable ? fixed.filter(value => validFixed(field, value)) : [] };
  });
}
function productUrlKey(value: string) {
  const url = safeUrl(value);
  if (!url) return value;
  url.hostname = url.hostname.replace(/^www\./, ''); url.hash = '';
  return url.href;
}
function latestProducts(items: ProductDiagnosisRecord[]) {
  const selected = new Map<string, ProductDiagnosisRecord>();
  const hasPolicy = (item: ProductDiagnosisRecord) => item.revision != null && item.selection?.revision === item.revision;
  const preferred = (item: ProductDiagnosisRecord, previous: ProductDiagnosisRecord) => {
    if ((item.status === 'closed') !== (previous.status === 'closed')) return item.status !== 'closed';
    if (hasPolicy(item) !== hasPolicy(previous)) return hasPolicy(item);
    const nextTime = Date.parse(item.created_at || ''), previousTime = Date.parse(previous.created_at || '');
    return Number.isFinite(nextTime) && Number.isFinite(previousTime) && nextTime > previousTime;
  };
  for (const item of items) {
    const key = productUrlKey(item.url), previous = selected.get(key);
    // Replacing a Map value preserves the URL's original workspace position.
    if (!previous || preferred(item, previous)) selected.set(key, item);
  }
  return [...selected.values()];
}
interface DiagnosisDraftState { revision: string | number | undefined; fields: DiagnosisSelectionField[]; saved: boolean; revisionNotice: boolean }
function Problem({ children }: { children: ReactNode }) { return <div className="diagnosis-error" role="alert"><TriangleAlert size={17} /><div>{children}</div></div>; }

function AddSupplierForm({ onAdded, onCancel }: { onAdded: (supplier: SupplierSession) => void; onCancel: () => void }) {
  const [name, setName] = useState(''), [website, setWebsite] = useState(''), [login, setLogin] = useState('');
  const [error, setError] = useState(''), [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError('');
    if (!name.trim()) { setError('Enter a supplier name.'); return; }
    const site = safeUrl(website.trim()), entry = login.trim() ? safeUrl(login.trim()) : site;
    if (!site || !entry) { setError('Use complete public HTTPS URLs without embedded credentials.'); return; }
    if (site.search || site.hash || entry.search || entry.hash || site.port || entry.port) { setError('Use HTTPS URLs without query parameters, fragments, or a custom port.'); return; }
    if (site.hostname.replace(/^www\./, '') !== entry.hostname.replace(/^www\./, '') || site.port !== entry.port) {
      setError('The login page must use the same supplier domain as the website; a www version is allowed.'); return;
    }
    setSaving(true);
    try {
      const payload: NewSupplierSession = { name: name.trim(), website_url: site.href, ...(login.trim() ? { login_url: entry.href } : {}) };
      const supplier = await post<SupplierSession>('/supplier-sessions', payload);
      onAdded(supplier);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setSaving(false); }
  };
  return <form className="diagnosis-add-supplier" aria-label="Add Product Supplier" onSubmit={submit} noValidate>
    <h3>Add Product Supplier</h3><p>Adding a supplier enables login and diagnosis. Automatic extraction depends on supplier support.</p>
    <div className="diagnosis-supplier-fields"><label>Supplier name<input autoFocus autoComplete="organization" maxLength={120} value={name} disabled={saving} onChange={event => { setName(event.target.value); setError(''); }} placeholder="e.g. My print supplier" /></label><label>Website URL<input type="url" autoComplete="url" maxLength={4096} value={website} disabled={saving} onChange={event => { setWebsite(event.target.value); setError(''); }} placeholder="https://supplier.example" /></label><label className="diagnosis-login-field">Login page URL <span>Optional</span><input type="url" autoComplete="off" maxLength={4096} value={login} disabled={saving} onChange={event => { setLogin(event.target.value); setError(''); }} placeholder={website.trim() || 'Uses the website URL when left blank'} /><small>Leave blank to open the website. Use the same domain for a separate login page.</small></label></div>
    {error && <Problem>{error}</Problem>}
    <div className="diagnosis-add-actions"><button className="button button-secondary button-small" type="button" disabled={saving} onClick={onCancel}>Cancel</button><button className="button button-primary button-small" type="submit" disabled={saving}>{saving ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}{saving ? 'Adding supplier…' : 'Add supplier'}</button></div>
  </form>;
}

export default function ProductDiagnosis({ onRunsCreated, onOpenRun }: { onRunsCreated?: (runs: Run[]) => void; onOpenRun?: (id: string) => void }) {
  const [sessions, setSessions] = useState<SupplierSession[]>([]), [sessionError, setSessionError] = useState('');
  const [addingSupplier, setAddingSupplier] = useState(false), [supplierAdded, setSupplierAdded] = useState('');
  const [productUrls, setProductUrls] = useState(''), [addProductsError, setAddProductsError] = useState(''), [addingProducts, setAddingProducts] = useState(false), [launchBusy, setLaunchBusy] = useState(false);
  const [history, setHistory] = useState<ProductDiagnosisRecord[]>([]), [historyError, setHistoryError] = useState('');
  const [url, setUrl] = useState(''), [record, setRecord] = useState<ProductDiagnosisRecord | null>(null);
  const [draft, setDraft] = useState<DiagnosisSelectionField[]>([]), [error, setError] = useState('');
  const [busy, setBusy] = useState(''), [sessionBusy, setSessionBusy] = useState(''), [loading, setLoading] = useState(true);
  const [saved, setSaved] = useState(false), [revisionNotice, setRevisionNotice] = useState(false);
  const [pollError, setPollError] = useState(''), [pollRevision, setPollRevision] = useState(0);
  const current = useRef<ProductDiagnosisRecord | null>(null), sequence = useRef(0);
  const drafts = useRef(new Map<string, DiagnosisDraftState>());
  const productOrder = useRef<string[]>([]);
  const activeDraft = useRef<{ id?: string; state: DiagnosisDraftState }>({ state: { revision: undefined, fields: [], saved: false, revisionNotice: false } });
  activeDraft.current = { id: record?.id, state: { revision: record?.revision, fields: draft, saved, revisionNotice } };
  if (record) drafts.current.set(record.id, activeDraft.current.state);
  const accept = useCallback((next: ProductDiagnosisRecord) => {
    const previous = current.current;
    const changed = !!previous && previous.id === next.id && previous.revision != null && next.revision != null && previous.revision !== next.revision;
    if (previous && activeDraft.current.id === previous.id) drafts.current.set(previous.id, activeDraft.current.state);
    if (!previous || previous.id !== next.id || previous.revision !== next.revision || previous.status !== 'ready') {
      const cached = drafts.current.get(next.id);
      if (cached && cached.revision === next.revision) {
        setDraft(cached.fields); setSaved(cached.saved); setRevisionNotice(cached.revisionNotice);
      } else {
        setDraft(draftFor(next)); setSaved(!!next.selection && next.selection.revision === next.revision);
        setRevisionNotice(changed || (!!cached && cached.revision !== next.revision) || (!!next.selection && next.selection.revision !== next.revision));
      }
    }
    current.current = next; setRecord(next); setPollError('');
    setHistory(items => items.some(item => item.id === next.id) ? items.map(item => item.id === next.id ? next : item) : [next, ...items]);
  }, []);
  const loadSessions = useCallback(async () => {
    try { const result = await api<{ items: SupplierSession[] }>('/supplier-sessions'); setSessions(result.items || []); setSessionError(''); }
    catch (failure) { setSessionError(errorMessage(failure)); }
  }, []);
  const loadHistory = useCallback(async () => {
    try { const result = await api<{ items: ProductDiagnosisRecord[] }>('/product-diagnoses'); setHistory(result.items || []); setHistoryError(''); }
    catch (failure) { setHistoryError(errorMessage(failure)); }
  }, []);
  useEffect(() => { void Promise.all([loadSessions(), loadHistory()]).finally(() => setLoading(false)); return () => { sequence.current++; }; }, [loadSessions, loadHistory]);
  useEffect(() => {
    if (!record || !pending(record)) return;
    let disposed = false; let timer: number | undefined;
    const controller = new AbortController(), id = record.id;
    const poll = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(poll, 2000); return; }
      try {
        const next = await api<ProductDiagnosisRecord>(`/product-diagnoses/${encodeURIComponent(id)}`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
        if (disposed || current.current?.id !== id) return;
        accept(next);
        if (pending(next)) timer = window.setTimeout(poll, 2000);
      } catch (failure) { if (!disposed) setPollError(errorMessage(failure)); }
    };
    timer = window.setTimeout(poll, 1200);
    return () => { disposed = true; window.clearTimeout(timer); controller.abort(); };
  }, [record?.id, record?.status, pollRevision, accept]);
  const sessionAction = async (session: SupplierSession, action: 'open' | 'check') => {
    setSessionBusy(`${session.id}:${action}`); setSessionError('');
    try { await post(`/supplier-sessions/${encodeURIComponent(session.id)}/${action}`, {}); await loadSessions(); }
    catch (failure) { setSessionError(errorMessage(failure)); }
    finally { setSessionBusy(''); }
  };
  const deleteSupplier = async (session: SupplierSession) => {
    if (!window.confirm(`Remove ${session.name} from your suppliers?`)) return;
    setSessionBusy(`${session.id}:delete`); setSessionError('');
    try { await del(`/supplier-sessions/${encodeURIComponent(session.id)}`); setSessions(items => items.filter(item => item.id !== session.id)); }
    catch (failure) { setSessionError(errorMessage(failure)); }
    finally { setSessionBusy(''); }
  };
  const parsed = safeUrl(url.trim());
  const supplier = parsed ? sessions.find(item => safeUrl(item.origin)?.hostname.replace(/^www\./, '') === parsed.hostname.replace(/^www\./, '') && safeUrl(item.origin)?.port === parsed.port) : undefined;
  const canAnalyze = !!parsed && supplier?.status !== 'opening' && !busy && !launchBusy && !sessionBusy;
  const analyze = async (event: FormEvent) => {
    event.preventDefault(); setError('');
    if (!parsed) { setError('Enter a complete public HTTPS product URL without embedded credentials.'); return; }
    if (!canAnalyze) { setError('Wait for the supplier browser to open, or refresh its status before analysis.'); return; }
    const version = ++sequence.current; setBusy('analyze'); setSaved(false); setRevisionNotice(false);
    try { const next = await post<ProductDiagnosisRecord>('/product-diagnoses', { url: parsed.href }); if (version === sequence.current) accept(next); }
    catch (failure) { if (version === sequence.current) setError(errorMessage(failure)); }
    finally { if (version === sequence.current) setBusy(''); }
  };
  const openPrevious = async (id: string) => {
    if (!id) return;
    const version = ++sequence.current; setBusy('history'); setError('');
    try { const next = await api<ProductDiagnosisRecord>(`/product-diagnoses/${encodeURIComponent(id)}`); if (version === sequence.current) { accept(next); setUrl(next.url); } }
    catch (failure) { if (version === sequence.current) setError(errorMessage(failure)); }
    finally { if (version === sequence.current) setBusy(''); }
  };
  const refresh = async () => {
    if (!record) return;
    const version = ++sequence.current; setBusy('refresh'); setError('');
    try { const next = await post<ProductDiagnosisRecord>(`/product-diagnoses/${encodeURIComponent(record.id)}/refresh`, {}); if (version === sequence.current) accept(next); }
    catch (failure) { if (version === sequence.current) setError(errorMessage(failure)); }
    finally { if (version === sequence.current) setBusy(''); }
  };
  const addProducts = async (event: FormEvent) => {
    event.preventDefault(); setAddProductsError('');
    const values: string[] = [], seen = new Set<string>();
    for (const value of productUrls.split(/\s+/).filter(Boolean)) { const key = productUrlKey(value); if (!seen.has(key)) { seen.add(key); values.push(value); } }
    if (!values.length) { setAddProductsError('Enter at least one product URL.'); return; }
    if (values.some(value => !safeUrl(value))) { setAddProductsError('Use one complete public HTTPS product URL per line, without embedded credentials.'); return; }
    const existing = latestProducts(history), failures: string[] = [];
    let first: ProductDiagnosisRecord | undefined;
    setAddingProducts(true);
    for (const value of values) {
      const found = existing.find(item => productUrlKey(item.url) === productUrlKey(value) && item.status !== 'closed');
      if (found) { first ??= found; continue; }
      try {
        const next = await post<ProductDiagnosisRecord>('/product-diagnoses', { url: safeUrl(value)!.href });
        setHistory(items => [next, ...items.filter(item => item.id !== next.id)]); existing.unshift(next); first ??= next;
      } catch (failure) { failures.push(`${value}: ${errorMessage(failure)}`); }
    }
    if (first) { accept(first); setUrl(first.url); }
    setAddProductsError(failures.join(' '));
    if (!failures.length) setProductUrls('');
    setAddingProducts(false);
  };
  const latest = latestProducts(history);
  for (const item of latest) { const key = productUrlKey(item.url); if (!productOrder.current.includes(key)) productOrder.current.push(key); }
  const workspaceProducts = latest.sort((a, b) => productOrder.current.indexOf(productUrlKey(a.url)) - productOrder.current.indexOf(productUrlKey(b.url)));
  const backgroundPending = history.some(item => item.id !== record?.id && pending(item));
  useEffect(() => {
    if (!backgroundPending) return;
    let disposed = false; let timer: number | undefined;
    const controller = new AbortController();
    const poll = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(poll, 2500); return; }
      try {
        const page = await api<{ items: ProductDiagnosisRecord[] }>('/product-diagnoses', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
        if (disposed) return;
        setHistory(items => [...items.map(item => page.items.find(next => next.id === item.id) || item), ...page.items.filter(next => !items.some(item => item.id === next.id))]);
        setHistoryError('');
        if (page.items.some(item => item.id !== current.current?.id && pending(item))) timer = window.setTimeout(poll, 2500);
      } catch (failure) { if (!disposed) setHistoryError(errorMessage(failure)); }
    };
    timer = window.setTimeout(poll, 1500);
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [backgroundPending, record?.id]);
  const update = (key: string, patch: Partial<DiagnosisSelectionField>) => { setDraft(items => items.map(item => item.key === key ? { ...item, ...patch } : item)); setSaved(false); };
  const fields = record?.fields || [];
  const missing = fields.filter(field => {
    const choice = draft.find(item => item.key === field.key);
    return field.configurable && (field.disabled && choice?.include !== false || choice?.include === false && !validFixedSelection(field, choice.fixed_values));
  });
  const excluded = draft.filter(field => !field.include).length;
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!record || record.status !== 'ready' || record.revision == null || missing.length) return;
    const version = ++sequence.current; setBusy('save'); setError(''); setSaved(false);
    const policy: DiagnosisSelection = { revision: record.revision, fields: draft.map(item => ({ ...item, fixed_values: item.include ? [] : item.fixed_values })) };
    try {
      const next = await post<ProductDiagnosisRecord>(`/product-diagnoses/${encodeURIComponent(record.id)}/selection`, policy);
      if (version !== sequence.current) return;
      accept(next);
      if (next.revision !== policy.revision || !next.selection || next.selection.revision !== next.revision) { setError('The page snapshot changed or the saved policy could not be confirmed. Review the current fields before saving again.'); return; }
      setDraft(draftFor(next)); setSaved(true); setRevisionNotice(false);
    } catch (failure) { if (version === sequence.current) setError(errorMessage(failure)); }
    finally { if (version === sequence.current) setBusy(''); }
  };
  const recordSession = sessions.find(item => item.id === record?.supplier_id), ready = record?.status === 'ready';
  return <div className="diagnosis-page">
    <div className="page-heading"><div><div className="eyebrow">REVIEW BEFORE EXTRACTION</div><h1>Product diagnosis</h1><p>Sign in, inspect a product, and decide which fields belong in your output.</p></div><ListChecks className="diagnosis-heading-icon" size={29} /></div>
    <section className="panel diagnosis-sessions" aria-labelledby="diagnosis-session-title"><div className="panel-top"><div className="panel-title"><span className="diagnosis-step">1</span><h2 id="diagnosis-session-title">Sign in to the supplier</h2></div><div className="diagnosis-supplier-toolbar"><button className="button button-secondary button-small" disabled={addingSupplier} onClick={() => { setAddingSupplier(true); setSupplierAdded(''); }}><Plus size={13} />Add Product Supplier</button><button className="text-link" disabled={!!sessionBusy} onClick={() => void loadSessions()}><RefreshCw size={13} />Refresh status</button></div></div>
      <p className="diagnosis-intro">Open the Hermes browser and sign in there. Hermes reuses that browser session; passwords are never entered or saved in this form. Supplier sessions can expire.</p>
      {sessionError && <Problem>Supplier status is unavailable; the displayed status may be out of date. {sessionError}</Problem>}
      {addingSupplier && <AddSupplierForm onCancel={() => setAddingSupplier(false)} onAdded={supplier => { setSessions(items => [...items.filter(item => item.id !== supplier.id), supplier]); setAddingSupplier(false); setSupplierAdded(supplier.name); }} />}
      {supplierAdded && <p className="diagnosis-supplier-confirmation" role="status">{supplierAdded} added. Open its browser below when you want to sign in.</p>}
      <div className="diagnosis-session-grid">{sessions.map(session => <article className="diagnosis-session" key={session.id} aria-label={`${session.name} session`}><div className="diagnosis-session-heading"><ShieldCheck size={19} /><strong>{session.name}</strong><span className={`status ${sessionError ? 'status-stale' : session.status === 'signed_in' ? 'status-complete' : 'status-pending'}`}>{sessionError ? 'Last known: ' : ''}{sessionLabels[session.status]}</span></div><p>{session.message || 'Use the supplier browser to confirm account access.'}</p><div className="diagnosis-session-actions"><button className="button button-secondary button-small" disabled={!!sessionBusy} onClick={() => void sessionAction(session, 'open')}>{sessionBusy === `${session.id}:open` ? <LoaderCircle size={13} className="spin" /> : <ExternalLink size={13} />}Open {session.name} browser</button><button className="button button-secondary button-small" disabled={!!sessionBusy} onClick={() => void sessionAction(session, 'check')}>{sessionBusy === `${session.id}:check` ? <LoaderCircle size={13} className="spin" /> : <Check size={13} />}Check sign-in</button>{session.custom && <button className="button button-secondary button-small button-danger" disabled={!!sessionBusy} onClick={() => void deleteSupplier(session)}>{sessionBusy === `${session.id}:delete` ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={13} />}Remove supplier</button>}</div></article>)}</div>
      {!sessions.length && <p className="diagnosis-intro">{loading ? 'Loading supplier sessions…' : 'No supplier sessions are available. Refresh status to retry.'}</p>}
    </section>
    <section className="panel diagnosis-url-panel" aria-labelledby="diagnosis-url-title"><div className="panel-top"><div className="panel-title"><span className="diagnosis-step">2</span><h2 id="diagnosis-url-title">Analyze a product URL</h2></div></div><form onSubmit={analyze} className="diagnosis-url-form" noValidate><label htmlFor="diagnosis-url">Product URL</label><div className="diagnosis-url-input"><input id="diagnosis-url" type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://www.printograph.com/products/business-cards/order" autoComplete="url" disabled={(!!busy || launchBusy) || pending(record)} /><button className="button button-primary" type="submit" disabled={!canAnalyze || pending(record)}>{busy === 'analyze' ? <LoaderCircle size={15} className="spin" /> : <ListChecks size={15} />}Analyze product</button></div><p>{!url.trim() ? 'Paste the URL after signing in above.' : !parsed ? 'Enter a complete public HTTPS URL without embedded credentials.' : !supplier ? 'Hermes will inspect this public HTTPS product page. If sign-in is needed, use the opened browser and refresh the diagnosis.' : supplier.status !== 'signed_in' ? `${supplier.name} sign-in is not confirmed. Analysis can inspect a public page; sign in if the supplier requests it.` : `Uses your ${supplier.name} browser session. No harvest starts from this page.`}</p></form>
      <form className="diagnosis-add-products" onSubmit={addProducts}><label>Product URLs<textarea aria-label="Product URLs to add" rows={3} value={productUrls} disabled={addingProducts || (!!busy || launchBusy)} onChange={event => setProductUrls(event.target.value)} placeholder="One public HTTPS product URL per line" /></label><div><p>Add multiple products, then review each tab or run the current settings as they are.</p><button className="button button-secondary button-small" disabled={addingProducts || (!!busy || launchBusy)} type="submit">{addingProducts ? <LoaderCircle size={13} className="spin" /> : <Plus size={13} />}{addingProducts ? 'Adding products…' : 'Add products'}</button></div>{addProductsError && <Problem>{addProductsError}</Problem>}</form>
      {workspaceProducts.length > 0 && <label className="diagnosis-history">Choose a product<select aria-label="Previous diagnoses" value={record?.id || ''} disabled={(!!busy || launchBusy) || pending(record)} onChange={event => void openPrevious(event.target.value)}><option value="">Choose a saved diagnosis</option>{workspaceProducts.map(item => <option key={item.id} value={item.id}>{item.title || safeUrl(item.url)?.pathname || 'Product'} · {diagnosisLabels[item.status]}</option>)}</select></label>}
      {historyError && <Problem>Previous diagnoses could not be loaded. {historyError} <button className="text-link" onClick={() => void loadHistory()}>Retry history</button></Problem>}
    </section>
    {workspaceProducts.length > 0 && <section className="panel diagnosis-workspace" aria-label="Product workspace"><div className="diagnosis-product-tabs" role="tablist" aria-label="Product reviews">{workspaceProducts.map((item, index) => <button key={item.id} id={`product-tab-${item.id}`} role="tab" type="button" aria-selected={record?.id === item.id} aria-controls="diagnosis-product-panel" tabIndex={record?.id === item.id || !record && index === 0 ? 0 : -1} disabled={(!!busy || launchBusy) || addingProducts} onClick={() => void openPrevious(item.id)} onKeyDown={event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? workspaceProducts.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + workspaceProducts.length) % workspaceProducts.length; document.getElementById(`product-tab-${workspaceProducts[nextIndex].id}`)?.focus(); void openPrevious(workspaceProducts[nextIndex].id); }}><strong>{item.title || safeUrl(item.url)?.pathname || 'Product'}</strong><small>{diagnosisLabels[item.status]}{drafts.current.has(item.id) && !drafts.current.get(item.id)?.saved ? ' · Draft' : ''}</small></button>)}</div><DiagnosisLaunch selectedId={record?.id} disabled={(!!busy || launchBusy) || addingProducts} products={workspaceProducts.map(item => { const entry = item.id === record?.id ? record : item; const cache = drafts.current.get(entry.id); const choices = entry.id === record?.id ? draft : cache && cache.revision === entry.revision ? cache.fields : draftFor(entry); return { record: entry, policy: entry.revision == null ? null : { diagnosis_id: entry.id, revision: entry.revision, fields: choices.map(choice => ({ ...choice, fixed_values: choice.include ? [] : choice.fixed_values })) } }; })} onCreated={onRunsCreated} onOpenRun={onOpenRun} onBusyChange={setLaunchBusy} /></section>}
    {error && <Problem>{error}</Problem>}
    {record && <div id="diagnosis-product-panel" role="tabpanel" aria-labelledby={`product-tab-${record.id}`} tabIndex={0}><section className="panel diagnosis-review" aria-labelledby="diagnosis-review-title"><div className="panel-top"><div className="panel-title"><span className="diagnosis-step">3</span><h2 id="diagnosis-review-title">Choose output fields</h2></div><span className={`status ${pollError ? 'status-stale' : ready ? 'status-observed' : 'status-pending'}`}>{pollError ? 'Last known: ' : ''}{diagnosisLabels[record.status]}</span></div>
      <div className="diagnosis-product"><div><h3>{record.title || 'Product snapshot'}</h3>{safeUrl(record.url) && <><a href={record.url} target="_blank" rel="noopener noreferrer" aria-label="Open source URL in a separate browser tab">{record.url}<ExternalLink size={11} /></a><small className="diagnosis-source-note">Source link opens separately; use the Hermes browser for sign-in and option changes.</small></>}</div><button className="button button-secondary button-small" onClick={() => void refresh()} disabled={(!!busy || launchBusy) || pending(record) || recordSession?.status === 'opening' || record.status === 'closed'}><RefreshCw size={14} className={busy === 'refresh' ? 'spin' : ''} />Refresh current page</button></div>
      <p className="diagnosis-intro">This is the current visible page, not every conditional option. Change selections in the supplier browser, then refresh here to review that state. Analysis does not verify prices or start a harvest.</p>
      {pollError && <Problem>Analysis updates stopped; the displayed snapshot is last known. {pollError} <button className="text-link" onClick={() => { setPollError(''); setPollRevision(value => value + 1); }}>Retry updates</button></Problem>}
      {pending(record) && !pollError && <div className="diagnosis-wait" role="status"><LoaderCircle size={19} className="spin" />Reading the current supplier page…</div>}
      {record.message && <p className="diagnosis-intro">{record.message}</p>}
      {record.status === 'closed' && <p className="diagnosis-attention">The owned product page is closed. Analyze the product URL again to open a new diagnosis.</p>}
      {record.status === 'login_required' && <p className="diagnosis-attention">Sign in in the Hermes browser and check sign-in above. If the supplier redirected you, return to the product page in that Hermes browser, then choose Refresh current page.</p>}
      {['attention', 'error'].includes(record.status) && <p className="diagnosis-attention">The snapshot needs attention. Review the source page and warnings, then retry with Refresh current page.</p>}
      {!!record.warnings?.length && <div className="diagnosis-warnings"><strong>Observed limitations</strong><ul>{record.warnings.map((warning, index) => <li key={index}>{warningText(warning)}</li>)}</ul></div>}
      {revisionNotice && <p className="diagnosis-revision" role="status">The page snapshot changed. Review the fields and fixed choices again; the earlier policy is not confirmed for this revision.</p>}
      {fields.some(needsSupplierBinding) && <p className="diagnosis-binding-note" role="status"><strong>Supplier adapter support needed.</strong> Some choices were captured as displayed labels or custom controls. {ready ? 'You can save this diagnosis, but automatic harvesting of those fields still needs supplier adapter support.' : 'Resolve the diagnosis warnings before saving. Automatic harvesting of these fields still needs supplier adapter support.'}</p>}
      {fields.length > 0 && <form onSubmit={save}><p className="diagnosis-policy-note">Checked fields appear in your output. Unchecked options must keep an explicit fixed choice for pricing and dependencies; they stay available internally. Unchecked content fields are omitted.</p><div className="diagnosis-fields">{fields.map(field => {
        const policy = draft.find(item => item.key === field.key) || { key: field.key, include: true, fixed_values: [] };
        const label = fieldLabel(field), choices = field.choices || [];
        const multi = multipleField(field), placeholder = placeholderFor(field);
        const invalid = missing.some(item => item.key === field.key);
        return <div className={`diagnosis-field ${policy.include ? '' : 'diagnosis-field-excluded'}`} key={field.key}><div className="diagnosis-field-top"><label className="diagnosis-include"><input type="checkbox" aria-label={`Include ${label}`} checked={policy.include} disabled={!ready || (!!busy || launchBusy) || !!pollError} onChange={event => update(field.key, { include: event.target.checked })} /><span>{label}</span></label><small>{field.configurable ? field.required ? 'Required by source' : 'Product option' : 'Content'}{field.disabled ? ' · currently unavailable' : ''}</small></div>
          {choices.length > 0 && <details className="diagnosis-choices"><summary>Observed choices ({choices.length})</summary><ul>{choices.map((choice, index) => <li key={`${choice.value}-${index}`}><span>{choice.label?.trim() || 'Label unavailable'}</span>{choice.disabled && <small>Unavailable</small>}{field.selected_values?.includes(choice.value) && <small>Selected on page</small>}</li>)}</ul></details>}
          {!policy.include && field.configurable && <div className="diagnosis-fixed"><label>Fixed {label}{choices.length > 0 ? <select aria-label={`Fixed ${label}`} multiple={!!multi} value={multi ? policy.fixed_values : policy.fixed_values[0] ?? placeholder} disabled={!ready || (!!busy || launchBusy) || !!pollError} onChange={event => update(field.key, { fixed_values: Array.from(event.target.selectedOptions, option => option.value).filter(value => value !== placeholder) })}>{!multi && <option value={placeholder}>Choose an observed value</option>}{choices.map((choice, index) => <option key={`${choice.value}-${index}`} value={choice.value} disabled={choice.disabled || choice.placeholder || !choice.label?.trim()}>{choice.label?.trim() || 'Label unavailable'}{choice.disabled ? ' · unavailable' : ''}</option>)}</select> : numericField(field) ? <input aria-label={`Fixed ${label}`} type="number" min={field.min ?? undefined} max={field.max ?? undefined} step={field.step || 'any'} value={policy.fixed_values[0] || ''} disabled={!ready || (!!busy || launchBusy) || !!pollError} onChange={event => update(field.key, { fixed_values: event.target.value ? [event.target.value] : [] })} /> : <span className="diagnosis-fixed-unavailable">No usable fixed choice was observed. Include this field or change the source page and refresh.</span>}</label><p>{invalid ? 'Choose a valid fixed value before saving.' : 'Kept internally at this choice; hidden from your output.'}{multi && ' Select each value you want to keep.'}</p></div>}
          {field.configurable && field.disabled && <p className="diagnosis-omit">This source control is unavailable. Keep its exact observed fixed choice when hiding it, or change the source page and refresh.</p>}
          {!policy.include && field.configurable && multi && !policy.fixed_values.length && Array.isArray(field.selected_values) && field.selected_values.length === 0 && <p className="diagnosis-omit">Keep no choices selected, as observed on the page.</p>}
          {!policy.include && !field.configurable && <p className="diagnosis-omit">Omitted from the output.</p>}
        </div>;
      })}</div><div className="diagnosis-save"><div><strong>{excluded} field{excluded === 1 ? '' : 's'} excluded from output</strong><p>{missing.length ? `${missing.length} option${missing.length === 1 ? ' needs' : 's need'} a valid fixed choice.` : 'Save this policy for the reviewed snapshot. No extraction starts.'}</p></div><button className="button button-primary" type="submit" disabled={!ready || (!!busy || launchBusy) || !!pollError || record.revision == null || missing.length > 0}>{busy === 'save' ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}Save field policy</button></div></form>}
      {ready && !fields.length && <p className="diagnosis-attention">No fields were captured. Review the supplier page and refresh before creating a policy.</p>}
      {saved && ready && !busy && <div className="diagnosis-saved" role="status"><CheckCircle2 size={19} /><div><strong>Field policy saved · {excluded} excluded from output</strong><p>Saved for this page revision. No harvest has started; prices remain unverified.</p></div></div>}
      {saved && !busy && record.selection?.revision === record.revision && record.supplier_id === 'b2sign' && record.url === 'https://www.b2sign.com/pole-banner-set' && <PricingPilot record={record} />}
      {saved && ready && !busy && record.revision != null && <SavedProductCheck key={`${record.id}:${record.revision}`} diagnosisId={record.id} revision={record.revision} />}
    </section></div>}
  </div>;
}

interface SavedProduct { run_id: string; run_name: string; product_id: string; title: string; row_count: number | null; source: 'matrix' | 'quotes' }
interface FinalizationReport { id: string; examined_rows: number; eligible_rows: number; held_rows: number; invalid_rows_removed: number; hidden_choice_rows_omitted: number; reasons: Record<string, number>; download_url: string | null; report_url: string; row_report_url?: string; source?: 'matrix' | 'quotes'; catalog_complete: false }
function SavedProductCheck({ diagnosisId, revision }: { diagnosisId: string; revision: string | number }) {
  const [products, setProducts] = useState<SavedProduct[]>([]), [selected, setSelected] = useState('');
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [report, setReport] = useState<FinalizationReport | null>(null), [refresh, setRefresh] = useState(0);
  const base = `/product-diagnoses/${encodeURIComponent(diagnosisId)}`;
  useEffect(() => {
    let disposed = false; const controller = new AbortController();
    setLoading(true); setError('');
    void api<{ items: SavedProduct[] }>(`${base}/saved-products`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) }).then(result => {
      if (disposed) return;
      setProducts(result.items || []); setSelected(result.items?.length === 1 ? '0' : '');
    }).catch(failure => { if (!disposed) setError(errorMessage(failure)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; controller.abort(); };
  }, [base, refresh]);
  const product = selected === '' ? null : products[Number(selected)];
  const finalize = async () => {
    if (!product) return;
    setBusy(true); setError(''); setReport(null);
    try {
      const next = await api<FinalizationReport>(`${base}/finalization`, { method: 'POST', body: JSON.stringify({ revision, run_id: product.run_id, product_id: product.product_id, source: product.source }), signal: AbortSignal.timeout(120000) });
      setReport(next);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  };
  const download = async (url: string, name: string) => { setError(''); try { await downloadExport({ name, url }, { timeoutMs: 120000 }); } catch (failure) { setError(errorMessage(failure)); } };
  return <section className="diagnosis-finalization" aria-labelledby="diagnosis-finalization-title"><h3 id="diagnosis-finalization-title">Check saved product rows</h3><p>Apply the saved field policy to existing data. This is an offline check; it does not scrape the supplier or start a harvest.</p>
    {loading ? <p>Loading saved products…</p> : products.length ? <div className="diagnosis-finalization-controls"><label>Saved product and run<select aria-label="Saved product and run" value={selected} disabled={busy} onChange={event => { setSelected(event.target.value); setReport(null); setError(''); }}><option value="">Choose one saved product</option>{products.map((item, index) => <option key={`${item.run_id}:${item.product_id}:${item.source}`} value={String(index)}>{item.title} · {item.run_name} · {item.source === 'matrix' ? 'Matrix candidates · count calculated during review' : `${item.row_count == null ? 'Unknown count of' : count(item.row_count)} captured rows`}</option>)}</select></label><button className="button button-secondary" disabled={!product || busy || loading} onClick={() => void finalize()}>{busy ? <LoaderCircle className="spin" size={14} /> : <ListChecks size={14} />}{busy ? 'Checking saved rows…' : 'Check saved rows'}</button></div> : <p>No saved rows are available for this diagnosis.</p>}
    {error && <Problem>{error}</Problem>}
    {!busy && !loading && <button className="text-link" onClick={() => { setReport(null); setRefresh(value => value + 1); }}>Refresh saved products</button>}
    {report && <div className="diagnosis-check-report" role="status"><dl><div><dt>Rows examined</dt><dd>{count(report.examined_rows)}</dd></div><div><dt>Eligible rows</dt><dd>{count(report.eligible_rows)}</dd></div><div><dt>Held for review</dt><dd>{count(report.held_rows)}</dd></div><div><dt>Invalid rows removed</dt><dd>{count(report.invalid_rows_removed)}</dd></div><div><dt>Other fixed choices omitted</dt><dd>{count(report.hidden_choice_rows_omitted)}</dd></div></dl><p>Held rows need review and are not automatically invalid. This check does not establish complete catalog coverage or independently verify supplier prices.</p>{Object.keys(report.reasons || {}).length > 0 && <details><summary>Review reasons</summary><ul>{Object.entries(report.reasons).map(([reason, total]) => <li key={reason}>{humanize(reason)}: {count(total)}</li>)}</ul></details>}<div className="diagnosis-report-actions"><button className="button button-secondary button-small" onClick={() => void download(report.report_url, 'product-row-check-report.json')}>Download check report</button>{report.row_report_url && <button className="button button-secondary button-small" onClick={() => void download(report.row_report_url!, 'product-row-by-row-report.ndjson')}>Download row-by-row report</button>}{report.download_url && <button className="button button-primary button-small" onClick={() => void download(report.download_url!, 'finalized-product-rows.csv')}>Download finalized rows</button>}</div>{!report.download_url && <p>No finalized row download is available for this report.</p>}</div>}
  </section>;
}
