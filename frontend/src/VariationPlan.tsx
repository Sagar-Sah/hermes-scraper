import { useEffect, useId, useState } from 'react';
import { Download, ExternalLink, ListChecks, RefreshCw, TriangleAlert } from 'lucide-react';
import { api, count, downloadExport, errorMessage, humanize, type VariationChoice, type VariationCounts, type VariationPlanPage, type VariationProductPlan } from './api';
import './VariationPlan.css';

const countLabel = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? count(value) : 'Unknown';
const columns: { key: keyof VariationCounts; label: string }[] = [
  { key: 'candidate_rows', label: 'Candidate rows' },
  { key: 'valid_rows', label: 'Valid rows' },
  { key: 'priced_rows', label: 'Priced rows' },
];
const inventoryGroups = [
  ['sizes', 'Sizes'], ['stocks', 'Stock'], ['colorspecs', 'Colorspec'], ['corners', 'Corners'], ['quantities', 'Quantity presets'],
] as const;

function choiceLabel(choice: number | VariationChoice): string {
  if (typeof choice === 'number') return Number.isFinite(choice) ? count(choice) : 'Unknown';
  return typeof choice?.label === 'string' && choice.label.trim() ? choice.label : 'Label unavailable';
}

function supplier(product: VariationProductPlan) {
  try {
    const url = new URL(product.product_url || '');
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.replace(/^www\./, '');
    return { url: url.href, host, label: host === 'printograph.com' ? 'Printograph' : host === 'printitprints.com' ? 'PrintItPrints' : host };
  } catch { return null; }
}

function candidateFilename(product: VariationProductPlan) {
  const name = product.title.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 80) || 'product';
  return `${name}-candidate-rows.csv`;
}

function Inventory({ product }: { product: VariationProductPlan }) {
  return <>
    <details className="variation-details"><summary>Option inventory</summary><div className="variation-inventory">
      {inventoryGroups.map(([key, label]) => {
        const choices = product.inventory?.[key];
        return <div key={key}><h4>{label}</h4>{choices?.length ? <ul>{choices.map((choice, index) => <li key={`${typeof choice === 'number' ? choice : choice.key || choice.label}-${index}`}>{choiceLabel(choice)}</li>)}</ul> : <p>{choices ? 'No choices recorded' : 'Not yet assessed'}</p>}</div>;
      })}
      {product.inventory?.other_dimensions?.map((dimension, index) => <div key={`${dimension.label}-${index}`}><h4>{dimension.label}</h4><ul>{dimension.choices.map((choice, choiceIndex) => <li key={choice.key || `${choice.label}-${choiceIndex}`}>{choiceLabel(choice)}</li>)}</ul>{dimension.notes && <p>{dimension.notes}</p>}</div>)}
    </div><p className="variation-note">Listed choices do not establish that every combination is available.</p></details>
    {!!product.non_cost_variants?.length && <details className="variation-details"><summary>Non-cost variants</summary><div className="variation-inventory">{product.non_cost_variants.map((variant, index) => <div key={`${variant.label}-${index}`}><h4>{variant.label}</h4><ul>{variant.choices.map((choice, choiceIndex) => <li key={choice.key || `${choice.label}-${choiceIndex}`}>{choiceLabel(choice)}</li>)}</ul>{variant.notes && <p>{variant.notes}</p>}</div>)}</div><p className="variation-note">Shown separately from the pricing plan. This view does not multiply them into price-row counts.</p></details>}
  </>;
}

export default function VariationPlan({ runId }: { runId: string }) {
  const [data, setData] = useState<VariationPlanPage | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [lastSuccess, setLastSuccess] = useState<Date | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<{ productId: string; message: string } | null>(null);
  const headingId = useId();
  useEffect(() => { setData(null); setSelectedId(''); setLastSuccess(null); }, [runId]);
  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const refresh = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(() => void refresh(), 15000); return; }
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 60000);
      setLoading(true);
      try {
        const next = await api<VariationPlanPage>(`/runs/${encodeURIComponent(runId)}/variation-plan`, { signal: controller.signal });
        if (!next || !Array.isArray(next.products) || next.products.some(product => !product || typeof product.product_id !== 'string')) throw new Error('Invalid variation-plan response.');
        if (disposed) return;
        setData(next); setError(''); setLastSuccess(new Date());
      } catch (failure) { if (!disposed) setError(errorMessage(failure)); }
      finally {
        window.clearTimeout(timeout);
        if (!disposed) { setLoading(false); timer = window.setTimeout(() => void refresh(), 15000); }
      }
    };
    void refresh();
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); };
  }, [runId, revision]);
  const product = data?.products.find(item => item.product_id === selectedId)
    || data?.products.find(item => item.product_type === '1' || item.product_url?.includes('/business-cards/') || item.title === 'Business Cards')
    || data?.products[0];
  const notes = [...(data?.notes || []), ...(product?.notes || [])];
  const source = product ? supplier(product) : null;
  const canExport = !!product && ['1', '27', '36'].includes(product.product_type || '') && source?.host === 'printograph.com';
  const downloadCandidates = async () => {
    if (!product) return;
    setDownloading(true); setDownloadError(null);
    try {
      await downloadExport({ name: candidateFilename(product), url: `/api/v1/runs/${encodeURIComponent(runId)}/variation-plan/export?product_id=${encodeURIComponent(product.product_id)}` }, { timeoutMs: 120000 });
    } catch (failure) { setDownloadError({ productId: product.product_id, message: errorMessage(failure) }); }
    finally { setDownloading(false); }
  };
  return <section className="panel variation-plan-panel" aria-labelledby={headingId}>
    <div className="panel-top"><div className="panel-title"><ListChecks size={18} /><h2 id={headingId}>Product variation plan</h2><span className="optional-badge">Inventory first</span></div><button className="icon-button bordered" aria-label="Refresh variation plan" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} className={loading ? 'spin' : ''} /></button></div>
    {error && <div className="variation-error" role="status"><TriangleAlert size={15} /><span>{data ? 'Plan updates unavailable · showing last-known inventory.' : 'Variation plan is unavailable.'}{lastSuccess && ` Last updated ${lastSuccess.toLocaleTimeString()}.`} <details><summary>Connection details</summary>{error}</details></span></div>}
    {!data && !error && <p className="variation-empty">Loading the observed options and row plan…</p>}
    {data && !product && <p className="variation-empty">An option inventory has not been recorded for this harvest yet.</p>}
    {product && <div className="variation-body"><div className="variation-product-heading"><div><h3>{product.title || 'Product'}</h3><p>Options first. Validate the combinations, then collect prices.</p><p className="variation-supplier">Supplier: {source ? <a href={source.url} target="_blank" rel="noopener noreferrer">{source.label}<ExternalLink size={11} /></a> : 'Not recorded'}</p></div>{data && data.products.length > 1 && <label>Product<select aria-label="Variation plan product" value={product.product_id} onChange={event => setSelectedId(event.target.value)}>{data.products.map(item => <option key={item.product_id} value={item.product_id}>{item.title || 'Product'}{supplier(item) ? ` · ${supplier(item)?.label}` : ''}</option>)}</select></label>}</div>
      {(product.status || notes.length > 0) && <div className="variation-coverage">{product.status && <span className="status">{humanize(product.status)}</span>}{notes.map((note, index) => <p key={index}>{note}</p>)}</div>}
      <dl className="variation-totals">{columns.map(column => <div key={column.key}><dt>{column.label}</dt><dd>{countLabel(product.counts?.[column.key])}</dd></div>)}</dl>
      {typeof product.table_subtotal_rows === 'number' && Number.isFinite(product.table_subtotal_rows) && product.table_subtotal_rows >= 0 && <p className="variation-note">{count(product.table_subtotal_rows)} candidates have calculated component subtotals; final price checks are pending.</p>}
      <p className="variation-note">Candidate matrix counts are separate from saved extraction rows. Candidates are possibilities, not validated combinations. Priced rows are not automatically verified prices. Unknown totals remain unknown.</p>
      {canExport && <div className="variation-export"><button className="button secondary compact" disabled={downloading} onClick={() => void downloadCandidates()}><Download size={14} />{downloading ? 'Preparing candidate rows…' : 'Download candidate rows'}</button><p>Pre-coupon calculated subtotals. These are not verified customer quotes.</p>{downloadError?.productId === product.product_id && <p role="alert">{downloadError.message}</p>}</div>}
      <div className="variation-size-heading"><h4>Row plan by size</h4><span>Counts supplied by the extraction plan</span></div>
      {product.size_plans?.length ? <div className="table-scroll variation-size-scroll" tabIndex={0} aria-label="Scroll size plans"><table className="data-table variation-size-table" aria-label={`${product.title || 'Product'} row plan by size`}><thead><tr><th>Size</th>{columns.map(column => <th key={column.key}>{column.label}</th>)}</tr></thead><tbody>{product.size_plans.map((size, index) => <tr key={size.key || `${size.label}-${index}`}><td>{size.label || 'Label unavailable'}{!!size.notes?.length && <small>{size.notes.join(' ')}</small>}</td>{columns.map(column => <td key={column.key} className="numeric">{countLabel(size[column.key])}</td>)}</tr>)}</tbody></table></div> : <p className="variation-empty">The per-size row plan is not yet assessed.</p>}
      <Inventory product={product} />
    </div>}
  </section>;
}
