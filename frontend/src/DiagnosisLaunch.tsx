import { useRef, useState } from 'react';
import { ArrowRight, LoaderCircle, Play, TriangleAlert } from 'lucide-react';
import { api, count, errorMessage, humanize, stableJson, type DiagnosisLaunchProduct, type DiagnosisLaunchResult, type DiagnosisPreflight, type ProductDiagnosisRecord, type Run } from './api';

interface LaunchAttempt { products: DiagnosisLaunchProduct[]; plan_token: string; idempotency_key: string; result?: DiagnosisLaunchResult }
const receiptStorageKey = 'hermes_diagnosis_launch_receipts_v1';
function restoredAttempts(): Map<string, LaunchAttempt> {
  const attempts = new Map<string, LaunchAttempt>();
  try {
    const stored = JSON.parse(sessionStorage.getItem(receiptStorageKey) || '[]');
    if (!Array.isArray(stored)) return attempts;
    for (const pair of stored.slice(-20)) {
      if (!Array.isArray(pair) || pair.length !== 2) continue;
      const [signature, entry] = pair;
      if (typeof signature !== 'string' || !entry || typeof entry.plan_token !== 'string' || !entry.plan_token ||
        typeof entry.idempotency_key !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(entry.idempotency_key) ||
        !Array.isArray(entry.products) || !entry.products.length || entry.products.some((product: DiagnosisLaunchProduct) => !product || typeof product.diagnosis_id !== 'string' ||
          !['string', 'number'].includes(typeof product.revision) || !Array.isArray(product.fields) || product.fields.some(field => !field || typeof field.key !== 'string' ||
            typeof field.include !== 'boolean' || !Array.isArray(field.fixed_values) || field.fixed_values.some(value => typeof value !== 'string'))) || stableJson(entry.products) !== signature) continue;
      const result = entry.result && typeof entry.result.batch_id === 'string' && Array.isArray(entry.result.runs) && entry.result.runs.every((run: Run) => run && typeof run.id === 'string' && typeof run.name === 'string' && typeof run.status === 'string') ? entry.result : undefined;
      attempts.set(signature, { products: entry.products, plan_token: entry.plan_token, idempotency_key: entry.idempotency_key, ...(result ? { result } : {}) });
    }
  } catch { /* A malformed receipt is never a reason to trust or launch it. */ }
  return attempts;
}
function retainAttempts(attempts: Map<string, LaunchAttempt>) {
  while (attempts.size > 20) {
    const completed = [...attempts].find(([, entry]) => entry.result);
    if (!completed) { attempts.delete([...attempts.keys()].at(-1)!); throw new Error('There are 20 unresolved launch receipts. Recover an earlier launch before starting another.'); }
    attempts.delete(completed[0]);
  }
  sessionStorage.setItem(receiptStorageKey, JSON.stringify([...attempts]));
}

export default function DiagnosisLaunch({ products, selectedId, disabled, onCreated, onOpenRun, onBusyChange }: {
  products: { record: ProductDiagnosisRecord; policy: DiagnosisLaunchProduct | null }[];
  selectedId?: string; disabled: boolean; onCreated?: (runs: Run[]) => void; onOpenRun?: (id: string) => void; onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(''), [error, setError] = useState('');
  const [plan, setPlan] = useState<DiagnosisPreflight | null>(null), [result, setResult] = useState<DiagnosisLaunchResult | null>(null);
  const inFlight = useRef(false);
  const attempt = useRef<Map<string, LaunchAttempt> | null>(null);
  if (!attempt.current) attempt.current = restoredAttempts();
  const run = async (all: boolean) => {
    if (inFlight.current) return;
    const chosen = all ? products : products.filter(item => item.record.id === selectedId);
    setError(''); setResult(null); setPlan(null);
    if (!chosen.length) { setError('Choose a product to run.'); return; }
    const payload = chosen.map(item => item.policy!);
    const signature = stableJson(payload);
    const unavailable = chosen.filter(item => item.record.status !== 'ready' || !item.policy);
    if (unavailable.length && !attempt.current!.has(signature)) { setError(`No products started. Review these products first: ${unavailable.map(item => item.record.title || 'Product').join(', ')}.`); return; }
    inFlight.current = true; onBusyChange?.(true); setBusy(all ? 'all' : 'one');
    try {
      const previous = attempt.current!.get(signature);
      if (previous?.result) { setResult(previous.result); return; }
      if (!previous) {
        const next = await api<DiagnosisPreflight>('/product-diagnoses/preflight', { method: 'POST', body: JSON.stringify({ products: payload }), signal: AbortSignal.timeout(60000) });
        setPlan(next);
        if (!next.ready) return;
        if (!next.plan_token) throw new Error('Preflight did not return a launch token. No launch was requested.');
        attempt.current!.set(signature, { products: payload, plan_token: next.plan_token, idempotency_key: crypto.randomUUID() });
      }
      const current = attempt.current!.get(signature)!;
      try { retainAttempts(attempt.current!); } catch { throw new Error('Launch recovery information could not be saved in this tab. No launch was requested; resolve browser storage or pending receipts before retrying.'); }
      const launched = await api<DiagnosisLaunchResult>('/product-diagnoses/launch', { method: 'POST', body: JSON.stringify({ products: current.products, plan_token: current.plan_token, idempotency_key: current.idempotency_key }), signal: AbortSignal.timeout(120000) });
      if (!launched || !Array.isArray(launched.runs) || !launched.batch_id) throw new Error('The launch result could not be confirmed. Retry to check the same launch safely.');
      current.result = launched;
      try { retainAttempts(attempt.current!); } catch { /* The pending receipt was already persisted and can recover this result. */ }
      setResult(launched); onCreated?.(launched.runs);
    } catch (failure) { setError(`${errorMessage(failure)} Retry uses the same launch key when the product choices are unchanged.`); }
    finally { inFlight.current = false; onBusyChange?.(false); setBusy(''); }
  };
  return <section className="diagnosis-launch" aria-label="Run configured products"><div><h3>Run your configured products</h3><p>Uses each product’s current field choices, including unsaved edits. All requested products must pass preflight before any run starts.</p></div><div className="diagnosis-launch-actions"><button className="button button-secondary button-small" disabled={disabled || !!busy || !selectedId} onClick={() => void run(false)}>{busy === 'one' ? <LoaderCircle size={14} className="spin" /> : <Play size={13} />}Run this product</button><button className="button button-primary button-small" disabled={disabled || !!busy || !products.length} onClick={() => void run(true)}>{busy === 'all' ? <LoaderCircle size={14} className="spin" /> : <Play size={13} />}Run all as configured ({products.length})</button></div><p className="diagnosis-launch-estimate">Pricing request count and completion time are unknown until the source adapter establishes them.</p>
    {error && <div className="diagnosis-error" role="alert"><TriangleAlert size={16} /><div>{error}</div></div>}
    {plan && !plan.ready && <div className="diagnosis-preflight" role="alert"><strong>No products started · preflight needs attention</strong><ul>{plan.products.filter(product => product.status === 'blocked').map(product => <li key={product.diagnosis_id}><strong>{product.title || 'Product'}</strong><ul>{product.reasons.map((reason, index) => <li key={index}>{/^[a-z0-9_]+$/.test(reason) ? humanize(reason) : reason}</li>)}</ul></li>)}</ul>{plan.note && <p>{plan.note}</p>}</div>}
    {result && <div className="diagnosis-launch-result" role="status"><strong>{count(result.runs.length)} product run{result.runs.length === 1 ? '' : 's'} created</strong><p>Runs share the Hermes engine and retain separate field policies. Their progress and price verification remain separate.</p>{result.runs.map(run => <button className="text-link" key={run.id} disabled={!onOpenRun} onClick={() => onOpenRun?.(run.id)}>{run.name} · {humanize(run.status)}<ArrowRight size={13} /></button>)}</div>}
  </section>;
}
