import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, RefreshCw, Terminal, TriangleAlert } from 'lucide-react';
import { api, dateTime, errorMessage, humanize, type EventPage, type RunEvent } from './api';

/** Bounded history pages keep old failures reachable without retaining every row event. */
export default function RunActivity({ runId, events, hasOlder, error }: {
  runId: string; events: RunEvent[]; hasOlder: boolean; error: string;
}) {
  const [category, setCategory] = useState<'all' | 'attention'>('all');
  const [before, setBefore] = useState<number | null>(null);
  const [page, setPage] = useState<EventPage | null>(null);
  const [pageError, setPageError] = useState('');
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const activeRequest = useRef(false);
  const needsPage = before !== null || category !== 'all';
  const load = useCallback(async () => {
    if (!needsPage || activeRequest.current) return;
    activeRequest.current = true;
    const current = ++sequence.current;
    setBusy(true);
    try {
      const result = await api<EventPage>(`/runs/${runId}/events?category=${category}${before === null ? '' : `&before=${before}`}`);
      if (current !== sequence.current) return;
      setPage(result); setPageError('');
    } catch (failure) {
      if (current === sequence.current) setPageError(errorMessage(failure));
    } finally {
      if (current === sequence.current) { activeRequest.current = false; setBusy(false); }
    }
  }, [runId, category, before, needsPage]);
  useEffect(() => {
    setPage(null); setPageError(''); setBusy(false); void load();
    const timer = window.setInterval(() => { if (!document.hidden && before === null) void load(); }, 3000);
    return () => { window.clearInterval(timer); sequence.current++; activeRequest.current = false; };
  }, [load, before]);
  const visible = needsPage ? page?.items || [] : events;
  const earlier = needsPage ? page?.has_older === true : hasOlder;
  const failure = pageError || error;
  return <div className="activity-panel">
    <div className="activity-toolbar">
      <label>Show <select aria-label="Activity category" value={category} onChange={event => { setBefore(null); setCategory(event.target.value as 'all' | 'attention'); }}>
        <option value="all">All activity</option><option value="attention">Errors, retries & gaps</option>
      </select></label>
      <span>{before === null ? 'Latest retained events · up to 250' : 'Historical page · live progress continues separately'}</span>
      {before !== null && <button className="text-link" onClick={() => setBefore(null)}>Return to latest</button>}
    </div>
    {failure && <div className="error-box" role="alert"><TriangleAlert size={16} /><span>Activity updates unavailable. Displayed events may be incomplete or out of date. {failure}</span></div>}
    {!visible.length && <p className="activity-empty">{busy ? 'Loading activity…' : failure ? 'Activity could not be loaded.' : 'No events in this view.'}</p>}
    <div className="event-log">{[...visible].reverse().map(event => <div className="event-item" key={event.id}>
      <span className="event-marker"><Terminal size={13} /></span><time>{dateTime(event.created_at)}</time>
      <div><strong>{humanize(event.type)}</strong><pre>{JSON.stringify(event.payload, null, 2)}</pre></div>
    </div>)}</div>
    <div className="activity-pagination">
      <button className="button button-secondary button-small" disabled={!earlier || !visible.length || busy} onClick={() => setBefore(visible[0].id)}><ArrowLeft size={14} />Load earlier events</button>
      <span>{visible.length} events shown{earlier ? ' · older history available' : ' · beginning of retained history'}</span>
      {needsPage && <button className="text-link" disabled={busy} onClick={() => void load()}><RefreshCw size={14} />Retry / refresh page</button>}
    </div>
  </div>;
}
