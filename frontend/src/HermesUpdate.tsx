import { useEffect, useId, useRef, useState } from 'react';
import { CheckCircle2, Download, LoaderCircle, RefreshCw, TriangleAlert, X } from 'lucide-react';
import { api } from './api';
import { canApplyUpdate, HermesUpdateClient, updateActive, updateComplete, updateReason, updateStage, type HermesUpdateView } from './hermesUpdateState';
import './HermesUpdate.css';

export default function HermesUpdate({ open, onClose, onChanged }: { open: boolean; onClose: () => void; onChanged?: () => void }) {
  const [client] = useState(() => new HermesUpdateClient(api));
  const [view, setView] = useState<HermesUpdateView>(client.view);
  const dialog = useRef<HTMLDialogElement>(null); const title = useId();
  const completed = useRef<string | null>(null);
  useEffect(() => client.subscribe(setView), [client]);
  useEffect(() => {
    const node = dialog.current;
    if (open) node?.showModal(); else node?.close();
  }, [open]);
  const active = updateActive(view.status);
  useEffect(() => {
    if (!open && !active && !view.reconnecting) return;
    let disposed = false; let timer: number | undefined;
    const poll = async () => { await client.refresh(); if (!disposed) timer = window.setTimeout(poll, client.view.reconnecting ? 5000 : 1500); };
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [client, open, active, view.reconnecting]);
  const complete = updateComplete(view.status);
  useEffect(() => {
    if (complete && view.status?.operation?.id !== completed.current) { completed.current = view.status?.operation?.id || null; onChanged?.(); }
  }, [complete, onChanged, view.status?.operation?.id]);
  const status = view.status; const busy = !!view.action || active;
  const failed = status?.operation?.status === 'failed';
  const reason = updateReason(status?.reason); const failure = updateReason(status?.operation?.error_code);
  return <dialog ref={dialog} className="hermes-update-dialog" aria-labelledby={title} onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2 id={title}>Update Hermes</h2><button className="icon-button" onClick={onClose} aria-label="Close Hermes update"><X size={19} /></button></header>
    <div className="hermes-update-body">
      <p>Install the latest checked release while preserving your collection, supplier sessions, and saved progress.</p>
      <div className="hermes-update-progress" role="status" aria-live="polite">
        {busy || view.reconnecting ? <LoaderCircle size={20} className="spin" /> : complete ? <CheckCircle2 size={20} /> : failed ? <TriangleAlert size={20} /> : <Download size={20} />}
        <strong>{view.reconnecting ? 'Reconnecting to Hermes' : updateStage(status, view.action)}</strong>
      </div>
      {status && <dl><div><dt>Current release</dt><dd>{status.current_commit?.slice(0, 12) || 'Not available'}</dd></div>{status.target_commit && <div><dt>Available release</dt><dd>{status.target_commit.slice(0, 12)}</dd></div>}</dl>}
      {reason && <p className="hermes-update-note">{reason}</p>}
      {status?.operation?.collection_paused && <p className="hermes-update-note">Collection is paused. Review saved tasks after the update before resuming.</p>}
      {failed && <div className="error-box" role="alert"><span>{failure || 'The update failed. Your saved server report is retained. Check again when the reported issue is resolved.'}</span></div>}
      {view.error && <div className="error-box" role="alert"><span>{view.error}</span></div>}
      {complete && <p className="hermes-update-note">Hermes has reconnected on the checked release. Reload the dashboard to load its updated controls.</p>}
      {status && !status.update_available && !busy && !failed && !complete && !reason && <p className="hermes-update-note">No newer checked release is currently recorded. Check for updates to refresh this result.</p>}
      {active && status?.operation?.kind === 'apply' && <p className="hermes-update-note">You can close this panel. The accepted update continues; reopening it shows saved progress.</p>}
    </div>
    <footer>
      <button className="button button-secondary" onClick={() => void client.refresh()} disabled={!!view.action}><RefreshCw size={14} />Refresh status</button>
      {complete ? <button className="button button-primary" onClick={() => window.location.reload()}>Reload dashboard</button> : canApplyUpdate(status) ? <button className="button button-primary" onClick={() => void client.apply()} disabled={busy || view.reconnecting}><Download size={14} />Install checked release</button> : <button className="button button-primary" onClick={() => void client.check()} disabled={busy || status?.can_check === false}><RefreshCw size={14} />{view.action === 'checking' ? 'Checking…' : 'Check for updates'}</button>}
    </footer>
  </dialog>;
}
