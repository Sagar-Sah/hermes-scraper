import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { CheckCircle2, LoaderCircle, Pause, Pencil, Play, Plus, RefreshCw, RotateCcw, Sparkles, Trash2, TriangleAlert, Zap } from 'lucide-react';
import Dialog from './Dialog';
import { api, errorMessage, type Provider } from './api';
import './ProviderManagement.css';

const mappingFields = ['path', 'body_template', 'prompt_field', 'model_field', 'response_path', 'auth_header', 'auth_prefix'] as const;
const defaultMapping = { path: '/generate', prompt_field: 'prompt', response_path: 'text', auth_header: 'Authorization', auth_prefix: 'Bearer ', body_template: {} };
type ProviderAction = 'test' | 'suspend' | 'resume' | 'delete' | 'restore';
export interface ProviderEditorValues { name: string; baseUrl: string; model: string; kind: string; key: string; custom: string }

export function providerStatus(provider: Provider): string { return provider.status || 'active'; }

export function providerEditorValues(provider?: Provider): ProviderEditorValues {
  const mapping = provider ? Object.fromEntries(mappingFields.filter(field => provider[field] !== undefined).map(field => [field, provider[field]])) : defaultMapping;
  return { name: provider?.name || '', baseUrl: provider?.base_url || '', model: provider?.model || '', kind: provider?.kind || 'openai_compatible', key: '', custom: JSON.stringify(mapping, null, 2) };
}

export function providerSaveRequest(values: ProviderEditorValues, provider?: Provider): { path: string; options: RequestInit } {
  if (provider && providerStatus(provider) === 'deleted') throw new Error('Restore this provider before editing it.');
  const mapping = values.kind === 'custom' ? JSON.parse(values.custom) : {};
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) throw new Error('Custom mapping must be a JSON object.');
  if (Object.keys(mapping).some(field => !mappingFields.includes(field as typeof mappingFields[number]))) throw new Error('Custom mapping contains an unsupported field. Put credentials only in the API key field.');
  const body = { ...mapping, name: values.name, base_url: values.baseUrl, model: values.model, kind: values.kind, ...(values.key.trim() ? { api_key: values.key } : {}) };
  return { path: provider ? `/providers/${encodeURIComponent(provider.id)}` : '/providers', options: { method: provider ? 'PUT' : 'POST', body: JSON.stringify(body) } };
}

export function providerActionRequest(provider: Provider, action: ProviderAction): { path: string; options: RequestInit } {
  const allowed = { active: ['test', 'suspend', 'delete'], suspended: ['resume', 'delete'], deleted: ['restore'] };
  if (!(allowed[providerStatus(provider) as keyof typeof allowed] || []).includes(action)) throw new Error('This action is unavailable for the provider’s current status. Refresh the list and try again.');
  return { path: `/providers/${encodeURIComponent(provider.id)}${action === 'delete' ? '' : `/${action}`}`, options: { method: action === 'delete' ? 'DELETE' : 'POST' } };
}

function ErrorBox({ message }: { message: string }) { return message ? <div className="error-box" role="alert"><TriangleAlert size={16} /><span>{message}</span></div> : null; }
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }

export function ProviderForm({ provider, onClose, onSaved }: { provider?: Provider; onClose: () => void; onSaved: (provider: Provider) => void }) {
  const [values, setValues] = useState(() => providerEditorValues(provider));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const close = useCallback(() => { if (!submitting.current) onClose(); }, [onClose]);
  const update = (name: keyof ProviderEditorValues, value: string) => setValues(current => ({ ...current, [name]: value }));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError('');
    try {
      const { path, options } = providerSaveRequest(values, provider);
      const saved = await api<Provider>(path, options);
      setValues(current => ({ ...current, key: '' }));
      onSaved(saved);
    } catch (error) { setError(values.key.trim() ? errorMessage(error).replaceAll(values.key, '[redacted]') : errorMessage(error)); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <Dialog title={provider ? 'Edit AI provider' : 'Connect an AI provider'} eyebrow="OPTIONAL INTELLIGENCE" onClose={close}>
    <form onSubmit={submit} className="settings-form provider-form" aria-busy={busy}>
      <p className="modal-intro">{provider ? 'Update this saved connection. Its ID and audit history stay the same. Saving does not call or test the model.' : 'Connect an OpenAI-compatible, Anthropic, or custom JSON API endpoint. Saving does not call the model.'}</p>
      {provider && <p className="provider-edit-status">Current status: <strong>{providerStatus(provider)}</strong>. Editing does not resume a suspended provider.</p>}
      <fieldset disabled={busy}>
        <Field label="Connection name"><input required value={values.name} onChange={event => update('name', event.target.value)} placeholder="My model provider" /></Field>
        <Field label="API protocol"><select value={values.kind} onChange={event => update('kind', event.target.value)}><option value="openai_compatible">OpenAI compatible</option><option value="anthropic">Anthropic Messages API</option><option value="custom">Custom JSON HTTP API</option></select></Field>
        <Field label="API base URL"><input type="url" required value={values.baseUrl} onChange={event => update('baseUrl', event.target.value)} placeholder="https://api.example.com/v1" /></Field>
        <Field label="Model ID"><input required value={values.model} onChange={event => update('model', event.target.value)} placeholder="Your provider’s exact model ID" /></Field>
        <Field label="API key" hint={provider ? 'Leave blank to keep the saved key. The current key is never displayed.' : 'Optional for endpoints without authentication. Keys are never returned in the provider list.'}><input type="password" autoComplete="new-password" spellCheck={false} value={values.key} onChange={event => update('key', event.target.value)} placeholder={provider ? 'Leave blank to keep saved key' : 'Enter your API key'} /></Field>
        {values.kind === 'custom' && <Field label="Request / response mapping · JSON" hint="Configure the endpoint path, prompt field, model field, response path, authentication header and static body. Keep credentials in the API key field."><textarea className="code-input" rows={9} required value={values.custom} onChange={event => update('custom', event.target.value)} /></Field>}
      </fieldset>
      <ErrorBox message={error} />
      <div className="modal-footer"><button className="button button-secondary" type="button" disabled={busy} onClick={close}>Cancel</button><button className="button button-primary" disabled={busy}>{busy ? <LoaderCircle size={15} className="spin" /> : provider ? <Pencil size={15} /> : <Plus size={15} />}{provider ? 'Save changes' : 'Save provider'}</button></div>
    </form>
  </Dialog>;
}

export function ProviderCard({ provider, busy, onEdit, onAction }: { provider: Provider; busy: string; onEdit: (provider: Provider) => void; onAction: (provider: Provider, action: ProviderAction) => void }) {
  const status = providerStatus(provider);
  const active = status === 'active', deleted = status === 'deleted';
  const actionIcon = (action: ProviderAction, icon: ReactNode) => busy === `${provider.id}:${action}` ? <LoaderCircle size={14} className="spin" /> : icon;
  return <div className={`provider-item provider-managed provider-${status}`}>
    <div className="product-glyph"><Sparkles size={20} /></div>
    <div className="provider-details"><div className="provider-name"><strong>{provider.name}</strong><span className={`provider-state provider-state-${status}`}>{status}</span></div><small>{provider.model} <span className="middot">·</span> {provider.base_url}</small>{!active && <small>{deleted ? 'Deleted · saved history retained. Restore first, then resume.' : 'Suspended · new and queued AI calls are blocked.'}</small>}</div>
    <div className="provider-actions" role="group" aria-label={`Manage ${provider.name}`}>
      <button className="button button-secondary button-small" disabled={!!busy || !active} onClick={() => onAction(provider, 'test')} title={active ? 'Sends a small model request; your provider may charge for it.' : 'Resume the provider before testing.'}>{actionIcon('test', <Zap size={14} />)}Test connection</button>
      {deleted ? <button className="button button-secondary button-small" disabled={!!busy} onClick={() => onAction(provider, 'restore')}>{actionIcon('restore', <RotateCcw size={14} />)}Restore</button> : <>
        <button className="button button-secondary button-small" disabled={!!busy} onClick={() => onEdit(provider)}><Pencil size={14} />Edit</button>
        {active ? <button className="button button-secondary button-small" disabled={!!busy} onClick={() => onAction(provider, 'suspend')}>{actionIcon('suspend', <Pause size={14} />)}Suspend</button> : <button className="button button-secondary button-small" disabled={!!busy || status !== 'suspended'} onClick={() => onAction(provider, 'resume')}>{actionIcon('resume', <Play size={14} />)}Resume</button>}
        <button className="button button-secondary button-small button-danger" disabled={!!busy} onClick={() => onAction(provider, 'delete')}><Trash2 size={14} />Delete</button>
      </>}
    </div>
  </div>;
}

export default function ProviderManagement() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [showDeleted, setShowDeleted] = useState(false);
  const [editing, setEditing] = useState<Provider | 'new' | null>(null);
  const [deleting, setDeleting] = useState<Provider | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const operation = useRef(false);
  const refresh = useCallback(async () => {
    const data = await api<{ items: Provider[] }>('/providers?include_deleted=true');
    setProviders(data.items || []); setLoaded(true);
  }, []);
  useEffect(() => { void refresh().catch(error => setError(errorMessage(error))); }, [refresh]);
  const closeEditor = useCallback(() => setEditing(null), []);
  const closeDelete = useCallback(() => { if (!operation.current) setDeleting(null); }, []);
  const act = async (provider: Provider, action: ProviderAction, confirmed = false) => {
    if (operation.current) return;
    if (action === 'delete' && !confirmed) { setError(''); setDeleting(provider); return; }
    operation.current = true; setBusy(`${provider.id}:${action}`); setError(''); setNotice('');
    let changed = false;
    try {
      const { path, options } = providerActionRequest(provider, action);
      const response = await api<Omit<Provider, 'status'> & { ok?: boolean; status?: string; error?: string; message?: string }>(path, options);
      if (response.ok === false || response.status === 'failed') throw new Error(String(response.error || response.message || 'Provider operation failed.'));
      if (action === 'test') setNotice(response.message || 'Provider connection test completed.');
      else {
        changed = true;
        setProviders(current => current.map(item => item.id === provider.id ? response as Provider : item));
        setDeleting(null);
        setNotice(action === 'restore' ? 'Provider restored and suspended. Review it, then Resume when ready.' : action === 'suspend' ? 'Provider suspended. New and queued calls are blocked; a call already in flight may finish.' : action === 'resume' ? 'Provider resumed. New AI calls can use it again.' : 'Provider deleted. Its audit history is retained, and it can be restored.');
        await refresh();
      }
    } catch (error) { setError(`${changed ? 'The change was saved, but the list could not refresh. ' : ''}${errorMessage(error)}`); }
    finally { operation.current = false; setBusy(''); }
  };
  const reload = async () => {
    if (operation.current) return;
    operation.current = true; setBusy('refresh'); setError('');
    try { await refresh(); } catch (error) { setError(errorMessage(error)); }
    finally { operation.current = false; setBusy(''); }
  };
  const deletedCount = providers.filter(provider => providerStatus(provider) === 'deleted').length;
  const visible = providers.filter(provider => showDeleted || providerStatus(provider) !== 'deleted');
  return <section className="panel sources-panel provider-management">
    <div className="panel-top"><div className="panel-title"><Sparkles size={19} /><h2>AI providers</h2><span className="optional-badge">Optional</span></div><button className="button button-secondary button-small" disabled={!!busy} onClick={() => { setEditing('new'); setNotice(''); }}><Plus size={14} />Connect provider</button></div>
    <p className="section-description">Edit, suspend or delete saved connections here. Collection and price arithmetic continue without a model. Suspension blocks new and queued AI calls; a call already in flight may finish.</p>
    <div className="provider-list-tools"><label><input type="checkbox" checked={showDeleted} onChange={event => setShowDeleted(event.target.checked)} />Show deleted{loaded ? ` (${deletedCount})` : ''}</label><button className="text-link" disabled={!!busy} onClick={() => void reload()}>{busy === 'refresh' ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}Refresh providers</button></div>
    <div className="provider-feedback"><ErrorBox message={error} />{notice && <div className="success-box" role="status"><CheckCircle2 size={17} />{notice}</div>}</div>
    <div className="provider-list" aria-busy={!loaded || !!busy}>{visible.map(provider => <ProviderCard key={provider.id} provider={provider} busy={busy} onEdit={provider => { setEditing(provider); setNotice(''); }} onAction={(provider, action) => void act(provider, action)} />)}{!visible.length && <p className="provider-empty">{!loaded ? error ? 'Provider list could not be loaded. Refresh to try again.' : 'Loading providers…' : 'No providers in this view. Connect a provider or show deleted connections to restore one.'}</p>}</div>
    {editing && <ProviderForm key={editing === 'new' ? 'new' : editing.id} provider={editing === 'new' ? undefined : editing} onClose={closeEditor} onSaved={saved => { setEditing(null); setProviders(current => [...current.filter(item => item.id !== saved.id), saved]); setNotice('Provider saved. No model request was sent.'); void refresh().catch(error => setError(`Provider saved, but the list could not refresh. ${errorMessage(error)}`)); }} />}
    {deleting && <Dialog title={`Delete ${deleting.name}?`} eyebrow="SAVED AI CONNECTION" onClose={closeDelete}><div className="settings-form provider-delete"><p className="modal-intro">This stops new and queued AI calls through this connection. A call already in flight may finish. Audit history and references are retained.</p><p className="modal-intro">You can restore it from <strong>Show deleted</strong>. Restored providers remain suspended until you choose Resume.</p><ErrorBox message={error} /><div className="modal-footer"><button className="button button-secondary" disabled={!!busy} onClick={closeDelete}>Cancel</button><button className="button button-secondary button-danger" disabled={!!busy} onClick={() => void act(deleting, 'delete', true)}>{busy ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}Delete provider</button></div></div></Dialog>}
  </section>;
}
