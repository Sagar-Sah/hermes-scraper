import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Archive, ArrowDownToLine, Check, CheckCircle2, ExternalLink, LoaderCircle, Package, RefreshCw, TriangleAlert, X } from 'lucide-react';
import { api, count, errorMessage, humanize } from './api';
import { cancelledFileChoice, folderPicker, saveCollectionDownload, type ExportDirectory } from './collectionDownloads';
import { collectionProductId, collectionProgress, completedProductIds, reconcileCollectionSelection, type CollectionArchive, type CollectionExport, type CollectionProduct, type CollectionSnapshot } from './collectionTrayState';
import './CollectionTray.css';

function TrayDialog({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null); const titleId = useId();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => { dialog?.close(); }; }, []);
  return <dialog ref={ref} className={`collection-dialog ${wide ? 'collection-dialog-wide' : ''}`} aria-labelledby={titleId} onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2 id={titleId}>{title}</h2><button className="icon-button" onClick={onClose} aria-label={`Close ${title}`}><X size={19} /></button></header>{children}
  </dialog>;
}
const title = (product: { title?: string; product_id?: string; id: string }) => product.title || `Product ${product.product_id || product.id}`;
const sizeText = (value?: number) => typeof value === 'number' ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value / 1024)} KB` : 'ZIP archive';

export default function CollectionTray({ onOpenRun, onChanged }: { onOpenRun?: (id: string) => void; onChanged?: () => void }) {
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<CollectionSnapshot>({ items: [], archives: [] });
  const [loaded, setLoaded] = useState(false); const [pollError, setPollError] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [refreshKey, setRefreshKey] = useState(0); const [busy, setBusy] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set()); const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const defaultCompleted = useRef(true); const previousCompleted = useRef<Set<string> | null>(null);
  const [unread, setUnread] = useState(0); const [pulse, setPulse] = useState(0); const [announcement, setAnnouncement] = useState('');
  const trayOpen = useRef(false); trayOpen.current = open;
  const [confirmUnfinished, setConfirmUnfinished] = useState<string[]>([]); const [applyAll, setApplyAll] = useState(false);
  const [pendingLocal, setPendingLocal] = useState<CollectionExport[]>([]); const receiptMutation = useRef(0);
  const [awaitingSave, setAwaitingSave] = useState<CollectionExport | null>(null);
  const [archiveReceipt, setArchiveReceipt] = useState<CollectionExport | null>(null); const [archiveSelection, setArchiveSelection] = useState<Set<string>>(new Set());
  const [discardReceipt, setDiscardReceipt] = useState<CollectionExport | null>(null); const [discardError, setDiscardError] = useState('');
  const [archiveError, setArchiveError] = useState(''); const [archiveFinalized, setArchiveFinalized] = useState(false);

  useEffect(() => {
    let disposed = false, inFlight = false, timer: number | undefined, failures = 0;
    const controller = new AbortController();
    const refresh = async () => {
      if (disposed || inFlight) return;
      if (document.hidden) { timer = window.setTimeout(refresh, 15000); return; }
      inFlight = true;
      try {
        const receiptVersion = receiptMutation.current;
        const next = await api<CollectionSnapshot>('/collection/products', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
        if (disposed) return;
        if (!Array.isArray(next.items) || !Array.isArray(next.archives)) throw new Error('Collection status was not recognized.');
        const complete = new Set(completedProductIds(next.items));
        if (previousCompleted.current !== null) {
          const added = [...complete].filter(id => !previousCompleted.current!.has(id));
          if (added.length) { setPulse(value => value + 1); setUnread(value => trayOpen.current ? 0 : value + added.length); setAnnouncement(`${count(added.length)} ${added.length === 1 ? 'product completed' : 'products completed'}.`); }
        }
        previousCompleted.current = complete;
        // A list begun before a completed create/finalize cannot erase its newer local receipt.
        if (receiptVersion === receiptMutation.current) setPendingLocal([]);
        setSnapshot(next); setSelected(value => reconcileCollectionSelection(value, next.items, defaultCompleted.current));
        setLoaded(true); setPollError(''); failures = 0;
      } catch (failure) { if (!disposed) { setPollError(errorMessage(failure)); failures += 1; } }
      finally { inFlight = false; if (!disposed) timer = window.setTimeout(refresh, Math.min(30000, 4000 * 2 ** Math.min(failures, 3))); }
    };
    const visible = () => { if (!document.hidden) { window.clearTimeout(timer); void refresh(); } };
    void refresh(); document.addEventListener('visibilitychange', visible);
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [refreshKey]);

  const close = useCallback(() => setOpen(false), []);
  const selectedProducts = snapshot.items.filter(product => selected.has(collectionProductId(product)));
  const completed = snapshot.items.filter(product => product.complete === true);
  const pending = new Map<string, CollectionExport>();
  for (const receipt of [...(snapshot.pending_exports || []), ...pendingLocal]) if (receipt.status !== 'finalized' || receipt.cleanup?.status === 'pending_private_cleanup') pending.set(receipt.id, receipt);
  const refresh = () => setRefreshKey(value => value + 1);
  const toggleProduct = (product: CollectionProduct) => {
    const id = collectionProductId(product); defaultCompleted.current = false;
    if (selected.has(id)) { setSelected(value => { const next = new Set(value); next.delete(id); return next; }); setAcknowledged(value => { const next = new Set(value); next.delete(id); return next; }); }
    else if (product.complete === true || acknowledged.has(id)) setSelected(value => new Set([...value, id]));
    else { setApplyAll(false); setConfirmUnfinished([id]); }
  };
  const confirmIncomplete = () => {
    const ids = applyAll ? snapshot.items.filter(product => product.complete !== true && product.exportable !== false).map(collectionProductId) : confirmUnfinished;
    setAcknowledged(value => new Set([...value, ...ids])); setSelected(value => new Set([...value, ...ids])); setConfirmUnfinished([]);
  };
  const startArchiveReview = (receipt: CollectionExport) => {
    if (!Array.isArray(receipt.products) || !receipt.products.length) throw new Error('The saved export has no product snapshot to review. Your collection was kept.');
    setArchiveSelection(new Set(receipt.products.map(collectionProductId))); setArchiveError(''); setArchiveFinalized(false); setArchiveReceipt(receipt);
  };
  const retryCleanup = (receipt: CollectionExport) => {
    if (receipt.status !== 'finalized' || receipt.cleanup?.status !== 'pending_private_cleanup' || !Array.isArray(receipt.finalize_selection)) { setError('The cleanup recovery details are unavailable. Refresh this collection.'); return; }
    setArchiveSelection(new Set(receipt.finalize_selection)); setArchiveError('The export choices were saved, but some files still need cleanup. Retry uses the same archive choices.'); setArchiveFinalized(true); setArchiveReceipt(receipt);
  };
  const saveReceipt = async (receipt: CollectionExport, directory?: ExportDirectory) => {
    const outcome = await saveCollectionDownload(receipt.url, receipt.filename, receipt.size, directory);
    if (outcome === 'saved') { setNotice('ZIP saved to your selected folder. Review which products to archive.'); startArchiveReview(receipt); }
    else { setAwaitingSave(receipt); setNotice('ZIP download started. No products have been removed.'); }
  };
  const exportSelected = async () => {
    if (!selectedProducts.length || busy) return;
    setBusy('export'); setError(''); setNotice('');
    try {
      const pick = folderPicker(); const directory = pick ? await pick() : undefined;
      const ids = selectedProducts.map(collectionProductId);
      const incomplete = selectedProducts.filter(product => product.complete !== true).map(collectionProductId);
      if (incomplete.some(id => !acknowledged.has(id))) throw new Error('Confirm each unfinished product before exporting.');
      const receipt = await api<CollectionExport>('/collection/exports', { method: 'POST', body: JSON.stringify({ product_ids: ids, allow_incomplete_product_ids: incomplete }), signal: AbortSignal.timeout(120000) });
      receiptMutation.current += 1; setPendingLocal(value => [...value.filter(item => item.id !== receipt.id), receipt]);
      await saveReceipt(receipt, directory);
    } catch (failure) { if (cancelledFileChoice(failure)) setNotice('Folder selection cancelled. Your collection was kept.'); else setError(errorMessage(failure)); }
    finally { setBusy(''); }
  };
  const retryDownload = async (receipt: CollectionExport) => {
    setBusy(`download:${receipt.id}`); setError('');
    try { const pick = folderPicker(); await saveReceipt(receipt, pick ? await pick() : undefined); }
    catch (failure) { if (!cancelledFileChoice(failure)) setError(errorMessage(failure)); }
    finally { setBusy(''); }
  };
  const downloadArchive = async (archive: CollectionArchive) => {
    setBusy(`archive:${archive.id}`); setError('');
    try {
      const url = archive.download_url || archive.url; if (!url) throw new Error('The archive download is unavailable.');
      const pick = folderPicker(); const outcome = await saveCollectionDownload(url, archive.filename, archive.size, pick ? await pick() : undefined);
      setNotice(outcome === 'saved' ? 'Archive saved to your selected folder.' : 'Archive ZIP download started.');
    } catch (failure) { if (!cancelledFileChoice(failure)) setError(errorMessage(failure)); }
    finally { setBusy(''); }
  };
  const discard = async () => {
    if (!discardReceipt || discardReceipt.status !== 'ready' || busy) return;
    setBusy('discard'); setDiscardError('');
    try {
      await api(`/collection/exports/${encodeURIComponent(discardReceipt.id)}/discard`, { method: 'POST', body: JSON.stringify({ discard: true }), signal: AbortSignal.timeout(120000) });
      receiptMutation.current += 1; setPendingLocal(value => value.filter(item => item.id !== discardReceipt.id));
      setSnapshot(value => ({ ...value, pending_exports: value.pending_exports?.filter(item => item.id !== discardReceipt.id) }));
      setNotice('Server ZIP discarded. The original collection was kept.'); setDiscardReceipt(null); refresh();
    } catch (failure) { setDiscardError(errorMessage(failure)); }
    finally { setBusy(''); }
  };
  const finalize = async () => {
    if (!archiveReceipt || busy) return;
    setBusy('finalize'); setArchiveError('');
    try {
      const result = await api<{ status: string; archived: CollectionArchive[]; deleted_product_ids: string[]; cleanup: { status: string } }>(`/collection/exports/${encodeURIComponent(archiveReceipt.id)}/finalize`, { method: 'POST', body: JSON.stringify({ saved: true, archive_product_ids: [...archiveSelection] }), signal: AbortSignal.timeout(120000) });
      if (result.status !== 'finalized') throw new Error('The cleanup result was not confirmed. Refresh the collection before trying again.');
      if (result.cleanup?.status === 'pending_private_cleanup') {
        const receipt = { ...archiveReceipt, status: 'finalized', finalize_selection: [...archiveSelection], cleanup: result.cleanup };
        receiptMutation.current += 1; setPendingLocal(value => [...value.filter(item => item.id !== receipt.id), receipt]); setArchiveFinalized(true);
        setArchiveError('The export choices were saved, but some files still need cleanup. Retry uses the same archive choices.'); refresh(); onChanged?.(); return;
      }
      setNotice(`${count(result.archived.length)} archived · ${count(result.deleted_product_ids.length)} removed after export.`);
      receiptMutation.current += 1; setPendingLocal(value => value.filter(item => item.id !== archiveReceipt.id)); setArchiveReceipt(null); refresh(); onChanged?.();
    } catch (failure) { setArchiveError(`Collection cleanup was not confirmed. Review the current collection before trying again. ${errorMessage(failure)}`); }
    finally { setBusy(''); }
  };

  return <div className="collection-tray-root">
    <button className="collection-tray-button" aria-label="Open collection" title="Collection and downloads" onClick={() => { setOpen(true); setUnread(0); }}>
      <span key={pulse} className={`collection-box-animation ${pulse ? 'collection-box-arrival' : ''}`}><svg width="30" height="30" viewBox="0 0 40 40" fill="none" aria-hidden="true"><g className="collection-falling-package"><path d="m16 6 5-3 5 3-5 3-5-3Z" fill="currentColor" opacity=".7" /><path d="M16 6v6l5 3V9m5-3v6l-5 3" stroke="currentColor" strokeWidth="1.8" /></g><g className="collection-open-box"><path d="m7 20 13-6 13 6-13 6L7 20Zm0 0v12l13 5 13-5V20M20 26v11" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><path d="m7 20-4-6 13-5 4 5m13 6 4-6-13-5-4 5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /></g></svg></span>
      {unread > 0 && <span className="collection-notification-count">{unread > 9 ? '9+' : unread}</span>}
    </button><span className="collection-sr-only" role="status" aria-live="polite">{announcement}</span>
    {open && <TrayDialog title="Your collection" onClose={close} wide><div className="collection-content">
      <p>Choose products to download as one ZIP. Complete products are selected by default. Source coverage and price verification remain separate.</p>
      {pollError && <div className="collection-error" role="alert"><TriangleAlert size={16} /><span>{loaded ? 'Showing last-known collection. ' : ''}{pollError}</span><button className="text-link" onClick={refresh}>Retry</button></div>}
      {error && <div className="collection-error" role="alert"><TriangleAlert size={16} /><span>{error}</span></div>}
      {notice && <p className="collection-notice" role="status">{notice}</p>}
      <div className="collection-selection"><button className="text-link" disabled={!!busy} onClick={() => { defaultCompleted.current = true; setSelected(new Set(completed.filter(product => product.exportable !== false).map(collectionProductId))); setAcknowledged(new Set()); }}>Select all completed</button><button className="text-link" disabled={!!busy} onClick={() => { defaultCompleted.current = false; setSelected(new Set()); setAcknowledged(new Set()); }}>Select none</button><span>{count(selectedProducts.length)} selected</span><button className="icon-button" aria-label="Refresh collection" disabled={!!busy} onClick={refresh}><RefreshCw size={15} /></button></div>
      {!loaded && !pollError && <p><LoaderCircle size={15} className="spin" /> Loading collection…</p>}
      <ul className="collection-product-list">{snapshot.items.map(product => { const id = collectionProductId(product), progress = collectionProgress(product); return <li key={id} className={product.complete === true ? 'collection-product-complete' : ''}>
        <input type="checkbox" aria-label={`Select ${title(product)}`} checked={selected.has(id)} disabled={!!busy || product.exportable === false} onChange={() => toggleProduct(product)} />
        <div className="collection-product-main"><strong>{title(product)}</strong><small>{product.run_name || 'Harvest'} · {count(product.row_count)} captured rows</small>
          {product.complete === true ? <span className="collection-complete-label"><CheckCircle2 size={13} />Complete</span> : <><span className="collection-product-status">{humanize(product.status === 'completed' ? 'incomplete' : product.status)} · Coverage {humanize(product.coverage_status || 'unknown')}</span><div className={`collection-progress ${progress === null ? 'collection-progress-indeterminate' : ''}`} role="progressbar" aria-label={`${title(product)} collection progress`} {...(progress === null ? {} : { 'aria-valuenow': Math.round(progress), 'aria-valuemin': 0, 'aria-valuemax': 100 })}><span style={progress === null ? undefined : { width: `${progress}%` }} /></div></>}
          {product.exportable === false && <small>Not available for export yet.</small>}
        </div>{product.complete === true ? <Check className="collection-complete-check" size={20} /> : <Package className="collection-pending-icon" size={19} />}
        {onOpenRun && <button className="icon-button" aria-label={`Open harvest for ${title(product)}`} onClick={() => { close(); onOpenRun(product.run_id); }}><ExternalLink size={14} /></button>}
      </li>; })}</ul>
      {loaded && !snapshot.items.length && <p className="collection-empty">No products in the collection yet.</p>}
      <div className="collection-export-actions"><span>{folderPicker() ? 'Choose a folder, then save the ZIP.' : 'Your browser chooses where to save the ZIP. Confirm it saved before reviewing cleanup.'}</span><button className="button button-primary" disabled={!!busy || !!pollError || !selectedProducts.length} onClick={() => void exportSelected()}>{busy === 'export' ? <LoaderCircle size={15} className="spin" /> : <ArrowDownToLine size={15} />}{busy === 'export' ? 'Preparing download…' : folderPicker() ? 'Choose folder & export' : 'Save ZIP…'}</button></div>
      {!!pending.size && <section className="collection-saved-section"><h3>Saved export downloads</h3><p>Ready ZIPs can be downloaded again if a save was interrupted. Cleanup-pending entries retry the already saved archive choices. No cleanup is automatic.</p>{[...pending.values()].map(receipt => <div className="collection-saved-item" key={receipt.id}><Archive size={17} /><div><strong>{receipt.filename}</strong><small>{sizeText(receipt.size)} · {receipt.status === 'finalized' ? 'File cleanup pending' : 'Awaiting export review'}</small></div><button className="button button-secondary button-small" disabled={!!busy} onClick={() => receipt.status === 'finalized' ? retryCleanup(receipt) : void retryDownload(receipt)}>{receipt.status === 'finalized' ? 'Retry cleanup' : 'Download ZIP'}</button>{receipt.status === 'ready' && <button className="text-link" disabled={!!busy} onClick={() => { setDiscardError(''); setDiscardReceipt(receipt); }}>Discard ZIP</button>}</div>)}</section>}
      <section className="collection-saved-section"><h3>Archived products</h3>{snapshot.archives.length ? snapshot.archives.map(archive => <div className="collection-saved-item" key={archive.id}><Archive size={17} /><div><strong>{archive.title || archive.filename}</strong><small>{sizeText(archive.size)} · Saved archive</small></div><button className="button button-secondary button-small" disabled={!!busy} onClick={() => void downloadArchive(archive)}>Download archive</button></div>) : <p>No archived products yet.</p>}</section>
    </div></TrayDialog>}
    {discardReceipt && <TrayDialog title="Discard server ZIP?" onClose={() => { if (!busy) setDiscardReceipt(null); }}><div className="collection-content"><p>Delete <strong>{discardReceipt.filename}</strong> from this server? The original collection stays, including its products and source rows. You can create a new export later.</p>{discardError && <p className="collection-error" role="alert">{discardError}</p>}<div className="collection-dialog-actions"><button className="button button-secondary" disabled={!!busy} onClick={() => setDiscardReceipt(null)}>Keep ZIP</button><button className="button button-primary" disabled={!!busy} onClick={() => void discard()}>Discard server ZIP</button></div></div></TrayDialog>}
    {!!confirmUnfinished.length && <TrayDialog title="Include unfinished products?" onClose={() => setConfirmUnfinished([])}><div className="collection-content"><p>These products are not complete. The ZIP may be missing options or prices, and does not establish complete supplier coverage.</p><ul>{snapshot.items.filter(product => confirmUnfinished.includes(collectionProductId(product))).map(product => <li key={collectionProductId(product)}>{title(product)}</li>)}</ul><label className="collection-confirm-check"><input type="checkbox" checked={applyAll} onChange={event => setApplyAll(event.target.checked)} />Select and acknowledge all unfinished products in this collection.</label><div className="collection-dialog-actions"><button className="button button-secondary" onClick={() => setConfirmUnfinished([])}>Keep selection unchanged</button><button className="button button-primary" onClick={confirmIncomplete}>Include unfinished products</button></div></div></TrayDialog>}
    {awaitingSave && <TrayDialog title="Did you save the ZIP?" onClose={() => setAwaitingSave(null)}><div className="collection-content"><p>Your browser started downloading <strong>{awaitingSave.filename}</strong>. Hermes cannot verify that it was saved. No collection cleanup has run.</p><p>Wait for the download to finish and confirm the ZIP is saved before continuing.</p><div className="collection-dialog-actions"><button className="button button-secondary" onClick={() => setAwaitingSave(null)}>Keep everything for now</button><button className="button button-primary" onClick={() => { const receipt = awaitingSave; setAwaitingSave(null); try { startArchiveReview(receipt); } catch (failure) { setError(errorMessage(failure)); } }}>I saved the ZIP</button></div></div></TrayDialog>}
    {archiveReceipt && <TrayDialog title="Keep exported products?" onClose={() => { if (!busy) setArchiveReceipt(null); }}><div className="collection-content">{archiveFinalized ? <p>Your archive choices have already been saved. Some private files could not yet be removed. Retrying cleanup keeps those choices unchanged.</p> : <><p>The ZIP has been saved. Checked products will be kept as downloadable archives. <strong>Unchecked exported products will be permanently removed from this collection and their saved source rows deleted.</strong></p><p>Active products and products changed since export are protected; the server will reject cleanup for those snapshots.</p></>}<div className="collection-selection"><button className="text-link" disabled={!!busy || archiveFinalized} onClick={() => setArchiveSelection(new Set(archiveReceipt.products.map(collectionProductId)))}>Select all</button><button className="text-link" disabled={!!busy || archiveFinalized} onClick={() => setArchiveSelection(new Set())}>Select none</button></div><ul className="collection-archive-options">{archiveReceipt.products.map(product => <li key={collectionProductId(product)}><label><input type="checkbox" checked={archiveSelection.has(collectionProductId(product))} disabled={!!busy || archiveFinalized} onChange={() => setArchiveSelection(value => { const next = new Set(value), id = collectionProductId(product); if (next.has(id)) next.delete(id); else next.add(id); return next; })} />Archive {title(product)}</label></li>)}</ul>{archiveError && <p className="collection-error" role="alert">{archiveError}</p>}<div className="collection-dialog-actions"><button className="button button-secondary" disabled={!!busy} onClick={() => setArchiveReceipt(null)}>{archiveFinalized ? 'Close · retry later' : 'Cancel · keep collection'}</button><button className="button button-primary" disabled={!!busy} onClick={() => void finalize()}>{busy === 'finalize' ? <LoaderCircle size={15} className="spin" /> : <Archive size={15} />}{archiveFinalized ? 'Retry cleanup' : 'Confirm archive / removal'}</button></div></div></TrayDialog>}
  </div>;
}
