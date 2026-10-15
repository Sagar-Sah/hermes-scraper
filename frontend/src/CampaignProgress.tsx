import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, ExternalLink, FileText, Layers3, LoaderCircle, RefreshCw, Search, TriangleAlert } from 'lucide-react';
import { api, count, dateTime, downloadExport, errorMessage, humanize, isActive, type CampaignDetail, type CampaignStage, type CampaignSummary } from './api';
import './CampaignProgress.css';
import B2HarvestProgress from './B2HarvestProgress';
import DPSPilotProgress from './DPSPilotProgress';

const stages: Record<CampaignStage, string> = { queued: 'Queued', diagnosed: 'Diagnosed', extracting: 'Extracting', extracted: 'Rows extracted', verified: 'Verified', finalized: 'Finalized', blocked: 'Blocked', excluded: 'Excluded' };
const active = (status: string) => isActive(status) || status === 'extracting';
function publicLink(value: string | null) {
  try { const url = new URL(value || ''); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
function SourceLink({ url, children }: { url: string | null; children: string }) {
  const href = publicLink(url);
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}<ExternalLink size={11} /></a> : null;
}

export default function CampaignProgress({ onOpenRun }: { onOpenRun: (id: string) => void }) {
  const [campaigns, setCampaigns] = useState<CampaignSummary[]>([]), [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [listLoading, setListLoading] = useState(true), [loading, setLoading] = useState(false);
  const [listError, setListError] = useState(''), [error, setError] = useState(''), [downloadError, setDownloadError] = useState('');
  const [query, setQuery] = useState(''), [stage, setStage] = useState<CampaignStage | 'all'>('all');
  const [groupFilter, setGroupFilter] = useState('active');
  const [refreshKey, setRefreshKey] = useState(0), [downloading, setDownloading] = useState('');
  const [lastSuccess, setLastSuccess] = useState<string | null>(null);
  const listVersion = useRef(0);
  const summaries = useRef(campaigns), detailId = useRef('');
  summaries.current = campaigns;
  const loadList = useCallback(async () => {
    const version = ++listVersion.current; setListLoading(true);
    try {
      const data = await api<{items:CampaignSummary[]}>('/campaigns');
      if (version !== listVersion.current) return;
      setCampaigns(data.items); setListError('');
      setSelected(previous => data.items.some(item => item.id === previous) ? previous : data.items[0]?.id || '');
    } catch (failure) { if (version === listVersion.current) setListError(errorMessage(failure)); }
    finally { if (version === listVersion.current) setListLoading(false); }
  }, []);
  useEffect(() => { void loadList(); return () => { listVersion.current++; }; }, [loadList]);
  useEffect(() => {
    if (!selected) { setLoading(false); return; }
    let disposed = false, timer: number | undefined;
    let shouldPoll = active(summaries.current.find(item => item.id === selected)?.status || '');
    const controller = new AbortController();
    if (detailId.current !== selected) { setError(''); setLastSuccess(null); }
    const refresh = async () => {
      if (disposed) return;
      if (document.hidden) { timer = window.setTimeout(refresh, 5000); return; }
      setLoading(true);
      try {
        const data = await api<CampaignDetail>(`/campaigns/${encodeURIComponent(selected)}`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
        if (disposed) return;
        setDetail(data); detailId.current = data.id; setError(''); setLastSuccess(new Date().toISOString()); shouldPoll = active(data.status);
        setCampaigns(items => items.map(item => item.id === data.id ? { ...item, ...data } : item));
      } catch (failure) { if (!disposed) setError(errorMessage(failure)); }
      finally { if (!disposed) { setLoading(false); if (shouldPoll) timer = window.setTimeout(refresh, 5000); } }
    };
    void refresh();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [selected, refreshKey]);
  const data = detail?.id === selected ? detail : null;
  const stale = !!error;
  const activeProducts = data?.products.filter(product => product.stage !== 'excluded') || [];
  const activeCount = data?.active_products ?? (data?.products.some(product => product.stage === 'excluded') ? activeProducts.length : data?.total_products ?? 0);
  const excludedCount = data?.excluded_products ?? (data?.products.length || 0) - activeProducts.length;
  const selectedGroup = groupFilter === 'active' ? data?.active_group_index : groupFilter === 'all' ? undefined : Number(groupFilter);
  const groupIds = selectedGroup == null ? null : new Set(data?.group_progress?.[selectedGroup]?.product_ids || []);
  const filtered = (data?.products || []).filter(product => (!groupIds || groupIds.has(product.id)) && (stage === 'all' || product.stage === stage) && `${product.title} ${product.supplier || ''} ${product.reason || ''} ${product.storefront_url} ${product.supplier_url || ''}`.toLowerCase().includes(query.trim().toLowerCase()));
  const refresh = () => { void loadList(); setRefreshKey(value => value + 1); };
  const report = async (id: string, source = false) => {
    setDownloading(id); setDownloadError('');
    try { await downloadExport({ name: source ? 'hermes-source-collection.json' : 'hermes-finalization-report.json', url: source ? `/api/v1/source-collections/${encodeURIComponent(id)}` : `/api/v1/finalizations/${encodeURIComponent(id)}/report` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const publicVariants = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-soardist-public-variants-${productId}.json`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/soardist-public-variants/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const fourOverDraft = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-${productId}-verification-pending.csv`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/fourover-draft/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const fourOverAreaDraft = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-${productId}-one-set-verification-pending.csv`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/fourover-area-draft/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const dpsRoutePrices = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-dps-scoped-prices-${productId}.json`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/dps-route-prices/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const b2signScopedDraft = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-b2sign-${productId}-verification-pending.csv`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/b2sign-scoped-draft/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  const fourOverShippingBaseline = async (productId: string) => {
    setDownloading(productId); setDownloadError('');
    try { await downloadExport({ name: `hermes-4over-shipping-baseline-${productId}.json`, url: `/api/v1/campaigns/${encodeURIComponent(selected)}/fourover-shipping-baseline/${encodeURIComponent(productId)}` }); }
    catch (failure) { setDownloadError(errorMessage(failure)); }
    finally { setDownloading(''); }
  };
  return <div className="campaign-page">
    <div className="page-heading"><div><div className="eyebrow">PRODUCT CAMPAIGNS</div><h1>Every product, through each stage.</h1><p>Follow the recorded inventory from diagnosis to finalized source data.</p></div><button className="button button-secondary" onClick={refresh} disabled={loading || listLoading}><RefreshCw size={15} className={loading || listLoading ? 'spin' : ''} />Refresh campaigns</button></div>
    {listError && <div className="error-box" role="alert"><TriangleAlert size={16} /><span>{campaigns.length ? 'Campaign list is last known.' : 'Campaigns are not available from this service yet.'} {listError}</span></div>}
    {campaigns.length > 0 && <div className="campaign-select"><label>Campaign<select aria-label="Choose campaign" value={selected} onChange={event => { setSelected(event.target.value); setQuery(''); setStage('all'); setGroupFilter('active'); setDownloadError(''); }}>{campaigns.map(item => <option key={item.id} value={item.id}>{item.name} · {count(item.active_products ?? item.total_products)} active products</option>)}</select></label>{data && <span className={`status ${stale ? 'status-stale' : `status-${data.status.replaceAll('_', '-')}`}`}>{stale ? 'Last known: ' : ''}{humanize(data.status)}</span>}</div>}
    {error && <div className="connection-banner" role="alert"><TriangleAlert size={17} /><div><strong>{data ? 'Live campaign updates unavailable · showing last-known data' : 'Campaign details are unavailable'}</strong><p>{lastSuccess ? `Last successful update: ${dateTime(lastSuccess)}. ` : ''}{error}</p></div></div>}
    {!data && <div className="panel empty-state"><div className="empty-icon">{loading || listLoading ? <LoaderCircle size={26} className="spin" /> : <Layers3 size={26} />}</div><h3>{loading || listLoading ? 'Loading campaign records' : campaigns.length ? 'No campaign details loaded' : listError ? 'Campaign service unavailable' : 'No campaigns recorded yet'}</h3><p>Campaigns appear here when an inventory is registered. Pricing pilots and fixture workloads do not count as campaign products.</p></div>}
    {data && <><dl className="campaign-metrics" aria-label={stale ? 'Last-known campaign counts' : 'Campaign counts'}>{(['diagnosed', 'extracted', 'verified', 'finalized', 'blocked'] as const).map(key => <div key={key}><dt>{humanize(key)}</dt><dd>{data.counts[key] == null ? 'Unknown' : count(data.counts[key])}<span> / {count(activeCount)}</span></dd></div>)}</dl><p className="campaign-scope">{excludedCount > 0 ? `${count(activeCount)} active products · ${count(data.original_total_products ?? data.total_products)} original entries · ${count(excludedCount)} excluded by request. Excluded records and evidence are retained; they do not count toward active progress.` : `${count(activeCount)} products in this campaign.`} Extraction rows, verified prices, and finalized output are separate stages. Pricing checks do not establish complete product coverage; no configuration total or completion ETA is inferred. Supplier shipping is reference data; PrintItPrints.com calculates shipping separately.</p>
      {!!data.group_progress?.length && <section className="panel campaign-group-panel" aria-label="Product completion groups"><div><strong>{data.active_group_index === data.group_progress.length ? 'All groups complete' : `Active group ${count((data.active_group_index ?? 0) + 1)} of ${count(data.group_progress.length)}`}</strong><p>{data.active_group_index === data.group_progress.length ? 'Every active product has a completed review.' : `${count(data.group_progress[data.active_group_index ?? 0]?.finalized_count ?? 0)} of ${count(data.group_progress[data.active_group_index ?? 0]?.required_count ?? data.group_progress[data.active_group_index ?? 0]?.product_count ?? 0)} included products finalized. Hermes holds the next group until every included product here is finalized.`}</p></div><label>View products<select aria-label="View product group" value={groupFilter} onChange={event => setGroupFilter(event.target.value)}><option value="active">Active group</option>{data.group_progress.map((group,index) => <option key={group.number} value={index}>Group {group.number} · {group.finalized_count}/{group.required_count ?? group.product_count} included finalized · {group.status}</option>)}<option value="all">All products</option></select></label></section>}
      {!!data.alternate_supplier_jobs?.length && <section className="panel" aria-label="Additional supplier scrapes"><h3>Additional supplier scrapes</h3>{data.alternate_supplier_jobs.map(job => <p key={job.id}><strong>{job.source_title}</strong> · {humanize(job.supplier)} · {humanize(job.stage)} for <SourceLink url={job.storefront_url}>{job.storefront_title}</SourceLink>{job.supplier_url ? <> · <SourceLink url={job.supplier_url}>Supplier page</SourceLink></> : <> · {job.reason || 'Supplier URL pending'}</>}</p>)}</section>}
      {data.source_pages_collected != null && <p className="campaign-source-count"><strong>{count(data.source_pages_collected)} / {count(activeCount)} supplier pages collected</strong><span>Public source evidence only; collection does not verify pricing or complete extraction.</span></p>}
      {data.public_variants_collected != null && <p className="campaign-source-count"><strong>{count(data.public_variants_collected)} Soardist product JSON snapshots</strong><span>Public options, variant prices, and image URLs; account prices, shipping, and production are not verified.</span></p>}
      {data.account_baselines_collected != null && <p className="campaign-source-count"><strong>{count(data.account_baselines_collected)} Soardist default prices checked in account</strong><span>Default configuration only; add-ons, quantities, shipping, and production remain separate checks.</span></p>}
      {data.fourover_default_probes_collected != null && <p className="campaign-source-count"><strong>{count(data.fourover_default_probes_collected)} 4over default configurations observed</strong><span>Quantity tiers from the account price route are scoped to one selected configuration per product; cart, shipping, and production remain unchecked.</span></p>}
      {data.fourover_price_tables_collected != null && <p className="campaign-source-count"><strong>{count(data.fourover_price_tables_collected)} 4over child price tables captured</strong><span>All source-declared child routes were checked for these products. Option labels, cart totals, shipping, and production still need validation.</span></p>}
      {data.fourover_option_maps_collected != null && <p className="campaign-source-count"><strong>{count(data.fourover_option_maps_collected)} 4over option maps completed</strong><span>Every source child ID was paired with its rendered customer-facing option choices.</span></p>}
      {data.fourover_area_drafts_collected != null && data.fourover_area_drafts_collected > 0 && <p className="campaign-source-count"><strong>{count(data.fourover_area_drafts_collected)} 4over area-price draft captured</strong><span>All rendered dimensions are priced at one set only; other quantities and final checks remain open.</span></p>}
      {data.fourover_rendered_canary_observations != null && data.fourover_rendered_canary_observations > 0 && <p className="campaign-source-count"><strong>{count(data.fourover_rendered_canary_observations)} 4over rendered-price checks saved</strong><span>These checks cover individual configurations; full option, cart, shipping, and production verification remain open.</span></p>}
      {data.itoris_option_tables_collected != null && <p className="campaign-source-count"><strong>{count(data.itoris_option_tables_collected)} Soardist source option tables captured</strong><span>Explicit option fees were read from the supplier-loaded calculator; combinations, quantities, shipping, and production remain to be checked.</span></p>}
      {data.itoris_quantity_tables_collected != null && <p className="campaign-source-count"><strong>{count(data.itoris_quantity_tables_collected)} Soardist quantity-tier tables captured</strong><span>Volume break rows are supplier-displayed tiers; final cart pricing and add-on behavior remain unchecked.</span></p>}
      {data.dps_route_price_scopes_collected != null && <p className="campaign-source-count"><strong>{count(data.dps_route_price_scopes_collected)} DPS products with scoped price quotes</strong><span>Selected dimensions, quantities, and individual option changes were quoted; combination availability, cart totals, shipping, and production still need checking.</span></p>}
      {data.b2sign_scoped_drafts_collected != null && data.b2sign_scoped_drafts_collected > 0 && <p className="campaign-source-count"><strong>{count(data.b2sign_scoped_draft_rows ?? 0)} B2Sign scoped draft rows across {count(data.b2sign_scoped_drafts_collected)} products</strong><span>These completed request scopes are downloadable per product; open quantities, cart totals, shipping, and production still need validation.</span></p>}
      {data.fourover_shipping_baselines_collected != null && data.fourover_shipping_baselines_collected > 0 && <p className="campaign-source-count"><strong>{count(data.fourover_shipping_baselines_collected)} 4over shipping selector baselines saved</strong><span>These rates are for one rendered configuration in the logged-in account; destination and rate dependencies still need verification.</span></p>}
      <p className="campaign-source-count"><strong>{count(activeProducts.filter(product => product.diagnosis_id).length)} / {count(activeCount)} supplier option snapshots linked</strong><span>Visible fields are a starting point; dependent choices and prices still require validation.</span></p>
      <B2HarvestProgress products={activeProducts} refreshToken={refreshKey} />
      <DPSPilotProgress products={activeProducts} refreshToken={refreshKey} />
      <section className="panel" aria-label="Campaign products"><div className="explorer-toolbar"><div className="search-field"><Search size={16} /><input aria-label="Search campaign products" placeholder="Search products, suppliers, or blockers…" value={query} onChange={event => setQuery(event.target.value)} /></div><select aria-label="Filter campaign stage" value={stage} onChange={event => setStage(event.target.value as CampaignStage | 'all')}><option value="all">All stages</option>{Object.entries(stages).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      {downloadError && <div className="error-box" role="alert"><TriangleAlert size={15} />{downloadError}</div>}
      <div className="table-scroll"><table className="data-table campaign-table"><thead><tr><th>Product</th><th>Supplier</th><th>Current stage</th><th>Price data</th><th>Attention / evidence</th><th>Updated</th></tr></thead><tbody>{filtered.map(product => <tr key={product.id}><td><strong>{product.title || 'Untitled product'}</strong><SourceLink url={product.storefront_url}>View product</SourceLink></td><td><span>{product.supplier || 'Supplier not linked'}</span><SourceLink url={product.supplier_url}>View supplier page</SourceLink></td><td><span className={`status ${stale ? 'status-stale' : `status-${product.stage}`}`}>{stale ? 'Last known: ' : ''}{stages[product.stage] || humanize(product.stage)}</span></td><td className="numeric">{product.finalization_evidence?.pricing_model === "source_live_quote" ? <>Live quote<br /><small>{count(product.finalization_evidence.quoted_canaries ?? 0)} verified samples</small></> : count(product.row_count)}</td><td><div className="campaign-evidence">{product.reason && <p className={product.stage === 'blocked' ? 'campaign-blocker' : ''}>{product.reason}</p>}{product.diagnosis_id && <span>{count(product.diagnosis_field_count ?? 0)} visible option fields saved{product.diagnosis_warnings?.length ? ` · ${product.diagnosis_warnings.join(', ')}` : ''}</span>}{product.account_baseline_evidence && <span>Account default price {humanize(product.account_baseline_evidence.comparison)}; other configurations pending.</span>}{product.fourover_price_table_evidence && <span>{count(product.fourover_price_table_evidence.priced_count)} / {count(product.fourover_price_table_evidence.child_count)} child price routes · {count(product.fourover_price_table_evidence.tier_count)} quantity prices; full-selection availability, production, and price review pending.</span>}{product.fourover_shipping_baseline_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void fourOverShippingBaseline(product.id)}><FileText size={12} />Download {count(product.fourover_shipping_baseline_evidence.method_count)} shipping methods · context pending</button>}{product.b2sign_scoped_draft_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void b2signScopedDraft(product.id)}><FileText size={12} />Download {count(product.b2sign_scoped_draft_evidence.row_count)} scoped B2Sign rows · verification pending</button>}{product.dps_route_price_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void dpsRoutePrices(product.id)}><FileText size={12} />Download {count(product.dps_route_price_evidence.quoted_count)} scoped DPS prices · verification pending</button>}{product.fourover_area_rows_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void fourOverAreaDraft(product.id)}><FileText size={12} />Download {count(product.fourover_area_rows_evidence.row_count)} one-set area draft rows</button>}{product.fourover_rows_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void fourOverDraft(product.id)}><FileText size={12} />Download {count(product.fourover_rows_evidence.row_count)} draft rows</button>}{product.run_id && <button className="text-link" onClick={() => onOpenRun(product.run_id!)}>View harvest <ArrowRight size={12} /></button>}{product.finalization_id && <button className="text-link" disabled={!!downloading} onClick={() => void report(product.finalization_id!)}>{downloading === product.finalization_id ? <LoaderCircle size={12} className="spin" /> : <FileText size={12} />}Finalization report</button>}{product.source_collection_id && <button className="text-link" disabled={!!downloading} onClick={() => void report(product.source_collection_id!, true)}><FileText size={12} />Source page evidence</button>}{product.public_variant_evidence && <button className="text-link" disabled={!!downloading} onClick={() => void publicVariants(product.id)}><FileText size={12} />{count(product.public_variant_evidence.variant_count)} public variants · prices unverified</button>}{!product.reason && !product.diagnosis_id && !product.run_id && !product.finalization_id && !product.source_collection_id && !product.public_variant_evidence && <span className="muted">No linked extraction or review yet</span>}</div></td><td className="campaign-date">{product.updated_at ? dateTime(product.updated_at) : 'Unknown'}</td></tr>)}</tbody></table></div>
      {!filtered.length && <div className="empty-state"><h3>{data.products.length ? 'No products match these filters' : 'No product entries returned'}</h3><p>{data.products.length ? 'Try another product, supplier, or stage.' : 'The campaign has no detailed product records available in this response.'}</p></div>}
      <div className="campaign-footer"><span>{count(filtered.length)} shown · {count(data.products.length)} recorded entries · {count(activeCount)} {excludedCount ? 'active products' : 'planned products'}{excludedCount > 0 && ` · ${count(excludedCount)} excluded`}</span><span>{stale ? 'Last-known update' : 'Campaign updated'}: {dateTime(data.updated_at)}</span></div></section></>}
  </div>;
}
