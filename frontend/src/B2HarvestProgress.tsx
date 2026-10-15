import { useEffect, useRef, useState } from 'react';
import { FileText, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react';
import { api, count, dateTime, downloadExport, errorMessage, humanize, type B2PricingHarvest, type CampaignProduct } from './api';

const active = (status: string) => ['opening', 'queued', 'running', 'harvesting', 'verifying'].includes(status);
function productKey(value: string | null) {
  try {
    const url = new URL(value || '');
    if (url.protocol !== 'https:' || !['b2sign.com', 'www.b2sign.com'].includes(url.hostname) || url.username || url.password) return null;
    return `https://www.b2sign.com${url.port ? `:${url.port}` : ''}${url.pathname.replace(/\/$/, '')}${url.search}`;
  } catch { return null; }
}

/** These finite pricing jobs are evidence, never campaign completion counters. */
export default function B2HarvestProgress({ products, refreshToken }: { products: CampaignProduct[]; refreshToken: number }) {
  const urlKey = JSON.stringify([...new Set(products.map(product => productKey(product.supplier_url)).filter((url): url is string => !!url))].sort());
  const urls = new Set<string>(JSON.parse(urlKey));
  const [reports, setReports] = useState<B2PricingHarvest[]>([]), [error, setError] = useState('');
  const [loading, setLoading] = useState(false), [revision, setRevision] = useState(0), [loaded, setLoaded] = useState(false);
  const [downloading, setDownloading] = useState(''), [downloadError, setDownloadError] = useState('');
  const lastReports = useRef(reports); lastReports.current = reports;
  useEffect(() => {
    if (urlKey === '[]') return;
    const currentUrls = new Set<string>(JSON.parse(urlKey));
    let disposed = false, timer: number | undefined;
    let shouldPoll = lastReports.current.some(report => currentUrls.has(productKey(report.url) || '') && active(report.status));
    const controller = new AbortController();
    const refresh = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(refresh, 5000); return; }
      setLoading(true);
      try {
        const response = await api<{items:B2PricingHarvest[]}>('/b2sign-harvests', {signal:AbortSignal.any([controller.signal, AbortSignal.timeout(12000)])});
        if (disposed) return;
        const matching = response.items.filter(report => currentUrls.has(productKey(report.url) || ''));
        setReports(matching); setLoaded(true); setError(''); shouldPoll = matching.some(report => active(report.status));
      } catch (failure) { if (!disposed) setError(errorMessage(failure)); }
      finally { if (!disposed) { setLoading(false); if (shouldPoll) timer = window.setTimeout(refresh, 5000); } }
    };
    void refresh();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [urlKey, revision, refreshToken]);
  const matching = reports.filter(report => urls.has(productKey(report.url) || ''));
  const download = async (id: string) => {
    setDownloading(id); setDownloadError('');
    try { await downloadExport({ name:'hermes-b2-pricing-harvest.json',url:`/api/v1/b2sign-harvests/${encodeURIComponent(id)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  if (!urls.size) return null;
  return <section className="panel campaign-pricing-panel" aria-label="B2Sign pricing harvests">
    <div className="panel-top"><div><h2>B2Sign pricing harvests</h2><p>Finite request sets for suppliers linked to this campaign.</p></div><button className="icon-button" aria-label="Refresh pricing harvests" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} className={loading ? 'spin' : ''} /></button></div>
    {error && <div className="campaign-pricing-warning" role="alert"><TriangleAlert size={15} /><span>{matching.length ? 'Pricing progress is last known; live updates are unavailable.' : 'Pricing harvest progress is unavailable.'} {error}</span></div>}
    {downloadError && <div className="campaign-pricing-warning" role="alert"><TriangleAlert size={15} />{downloadError}</div>}
    {!matching.length && !error && <p className="campaign-pricing-note">{loading && !loaded ? 'Loading pricing harvests…' : 'No pricing harvests are recorded for these supplier URLs.'}</p>}
    {matching.map(report => {
      const pending = report.verification_status.startsWith('pending');
      const status = report.status === 'completed' && report.finite_requested_set_exhausted ? 'Requested set processed' : humanize(report.status);
      return <article className="campaign-pricing-job" key={report.id} aria-label={report.title}><div className="campaign-pricing-heading"><strong>{report.title}</strong><span className={`status ${error ? 'status-stale' : active(report.status) ? 'status-running' : 'status-pending'}`}>{error ? 'Last known: ' : ''}{status}</span></div>
        <dl className="campaign-pricing-counts"><div><dt>Requested configurations</dt><dd>{count(report.requested_count)}</dd></div><div><dt>Checked configurations</dt><dd>{count(report.completed_count)}</dd></div><div><dt>{pending ? 'Quoted rows · held for verification' : 'Quoted rows'}</dt><dd>{count(report.row_count)}</dd></div><div><dt>Unavailable responses</dt><dd>{count(report.invalid_count)}</dd></div><div><dt>Quote requests</dt><dd>{count(report.quote_requests)}</dd></div></dl>
        <p className="campaign-pricing-note"><strong>{pending ? 'Pending independent price checks.' : `Verification: ${humanize(report.verification_status)}.`}</strong> {report.finite_requested_set_exhausted ? 'The requested set was processed. ' : 'The requested set has not been exhausted. '}Whole-product coverage and finalization are not established.</p>
        {!!report.warnings?.length && <details className="campaign-pricing-note"><summary>Recorded limitations</summary><ul>{report.warnings.map((warning,index) => <li key={index}>{humanize(warning)}</li>)}</ul></details>}
        <div className="campaign-pricing-footer"><span>{report.updated_at ? `Updated ${dateTime(report.updated_at)}` : 'Update time unavailable'}</span><button className="text-link" disabled={!!downloading} onClick={() => void download(report.id)}>{downloading === report.id ? <LoaderCircle size={13} className="spin" /> : <FileText size={13} />}Pricing harvest report</button></div>
      </article>;
    })}
    <p className="campaign-pricing-note campaign-pricing-footnote">These counts remain separate from the campaign’s extracted, verified, and finalized products. A completed request set is not a completed product catalog.</p>
  </section>;
}
