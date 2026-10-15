import { useEffect, useState } from 'react';
import { FileText, RefreshCw, TriangleAlert } from 'lucide-react';
import { api, count, dateTime, downloadExport, errorMessage, humanize, type CampaignProduct, type DPSPricingPilot } from './api';

const active = (status: string) => ['opening', 'running'].includes(status);
function key(value: string | null) {
  try {
    const url = new URL(value || '');
    if (url.protocol !== 'https:' || !['digital-print-solutions.com', 'www.digital-print-solutions.com'].includes(url.hostname)) return null;
    return url.pathname.replace(/\/$/, '');
  } catch { return null; }
}

export default function DPSPilotProgress({ products, refreshToken }: { products: CampaignProduct[]; refreshToken: number }) {
  const paths = [...new Set(products.map(product => key(product.supplier_url)).filter((path): path is string => !!path))].sort();
  const pathKey = JSON.stringify(paths);
  const [jobs, setJobs] = useState<DPSPricingPilot[]>([]), [error, setError] = useState('');
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(false), [downloadError, setDownloadError] = useState('');
  useEffect(() => {
    if (!paths.length) return;
    let disposed = false, timer: number | undefined;
    const controller = new AbortController();
    const refresh = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(refresh, 5000); return; }
      setLoading(true);
      try {
        const response = await api<{items:DPSPricingPilot[]}>('/dps-pilots', {signal:AbortSignal.any([controller.signal, AbortSignal.timeout(12000)])});
        if (disposed) return;
        const matching = response.items.filter(job => paths.includes(key(job.url) || ''));
        setJobs(matching); setError('');
        if (matching.some(job => active(job.status))) timer = window.setTimeout(refresh, 5000);
      } catch (failure) { if (!disposed) setError(errorMessage(failure)); }
      finally { if (!disposed) setLoading(false); }
    };
    void refresh();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [pathKey, revision, refreshToken]);
  if (!paths.length) return null;
  const latest = new Map<string, DPSPricingPilot>();
  for (const job of jobs) { const path = key(job.url); if (path && !latest.has(path)) latest.set(path, job); }
  const matching = [...latest.values()];
  const download = async (id: string) => {
    setDownloadError('');
    try { await downloadExport({name:'hermes-dps-pricing-pilot.json',url:`/api/v1/dps-pilots/${encodeURIComponent(id)}`}); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
  };
  return <section className="panel campaign-pricing-panel" aria-label="Digital Print Solutions pricing pilots">
    <div className="panel-top"><div><h2>Digital Print Solutions pricing pilots</h2><p>Bounded source quote checks for mapped products.</p></div><button className="icon-button" aria-label="Refresh DPS pilots" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} className={loading ? 'spin' : ''} /></button></div>
    {error && <div className="campaign-pricing-warning" role="alert"><TriangleAlert size={15} />Pricing pilot progress is unavailable. {error}</div>}
    {downloadError && <div className="campaign-pricing-warning" role="alert"><TriangleAlert size={15} />{downloadError}</div>}
    {!matching.length && !error && <p className="campaign-pricing-note">{loading ? 'Loading pricing pilots…' : 'No pricing pilots recorded for these supplier pages.'}</p>}
    {matching.map(job => <article className="campaign-pricing-job" key={job.id}><div className="campaign-pricing-heading"><strong>{products.find(product => key(product.supplier_url) === key(job.url))?.title || job.url}</strong><span className={`status ${active(job.status) ? 'status-running' : 'status-pending'}`}>{humanize(job.status)}</span></div>
      <dl className="campaign-pricing-counts"><div><dt>Requested quantities</dt><dd>{job.requested_quantities.join(', ') || 'Initial only'}</dd></div><div><dt>Matched source observations</dt><dd>{count(job.observations.length)}</dd></div><div><dt>Native quote requests</dt><dd>{count(job.quote_requests)}</dd></div></dl>
      <p className="campaign-pricing-note">One selected configuration was checked. Other options, full quantity coverage, shipping, and final product verification remain open.</p>
      {!!job.warnings?.length && <p className="campaign-pricing-warning">{job.warnings.map(humanize).join(', ')}</p>}
      <div className="campaign-pricing-footer"><span>{job.updated_at ? `Updated ${dateTime(job.updated_at)}` : 'Update time unavailable'}</span><button className="text-link" onClick={() => void download(job.id)}><FileText size={13} />Pricing pilot report</button></div>
    </article>)}
  </section>;
}
