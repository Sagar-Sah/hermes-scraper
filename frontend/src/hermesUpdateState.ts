export interface HermesUpdateOperation {
  id: string;
  kind: 'check' | 'apply';
  status: 'preparing' | 'running' | 'completed' | 'failed';
  stage: string;
  error_code: string | null;
  updated_at: number;
  collection_paused: boolean;
}
export interface HermesUpdateStatus {
  managed: boolean;
  can_check: boolean;
  can_apply: boolean;
  current_commit: string | null;
  target_commit: string | null;
  update_available: boolean;
  check_id: string | null;
  reason: string | null;
  operation: HermesUpdateOperation | null;
}
export interface HermesUpdateView {
  status: HermesUpdateStatus | null;
  action: 'checking' | 'applying' | null;
  error: string;
  reconnecting: boolean;
  errorKind: 'action' | 'connection' | null;
}
export const updateActive = (s: HermesUpdateStatus | null) => !!s?.operation && ['preparing', 'running'].includes(s.operation.status);
const commit = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{40}$/.test(v);
export function canApplyUpdate(s: HermesUpdateStatus | null): s is HermesUpdateStatus & { target_commit: string; check_id: string } {
  return !!s && s.managed === true && s.can_apply === true && s.update_available === true && !updateActive(s)
    && commit(s.target_commit) && typeof s.check_id === 'string' && s.check_id.length > 0 && s.check_id.length <= 200;
}
export const updateComplete = (s: HermesUpdateStatus | null) => !!s && s.operation?.kind === 'apply' && s.operation.status === 'completed' && commit(s.target_commit) && s.current_commit === s.target_commit;
export function updateStage(s: HermesUpdateStatus | null, action: HermesUpdateView['action']): string {
  if (action === 'checking') return 'Checking for updates';
  if (action === 'applying') return 'Starting update';
  const op = s?.operation;
  const stage = op?.stage;
  const stages: Record<string, string> = { checking: 'Checking for updates', preparing: 'Preparing update', draining: 'Finishing active work', backing_up: 'Saving your collection', stopping: 'Stopping the current service', updating: 'Installing release files', installing: 'Installing dependencies', building: 'Building the dashboard', restarting: 'Restarting Hermes', completed: 'Update complete', failed: 'Update failed' };
  if (op?.status === 'failed') return op.kind === 'check' ? 'Update check failed' : 'Update failed';
  if (op?.status === 'completed') return op.kind === 'check' ? (s?.update_available ? 'Update available' : 'Hermes is up to date') : updateComplete(s) ? 'Update complete' : 'Waiting for the updated service';
  if (stage && Object.hasOwn(stages, stage)) return stages[stage];
  return updateActive(s) ? 'Updating Hermes' : s?.update_available ? 'Update available' : 'Hermes updates';
}
export function updateReason(code: string | null | undefined): string {
  if (!code) return '';
  const known: Record<string, string> = {
    managed_restart_required: 'Relaunch Hermes once with the updated local launcher to enable updates from this screen.',
    dirty_checkout: 'Local source changes need to be saved before updating. Hermes will keep them intact.',
    working_tree_dirty: 'Local source changes need to be saved before updating. Hermes will keep them intact.',
    workers_busy: 'Hermes is still saving active work. Wait for it to finish, then check again.',
    external_database_backup_required: 'This deployment needs a verified database backup before it can update.',
    check_expired: 'This release check expired. Check for updates again.',
    no_update: 'Hermes is already on the current tested release.',
    check_required: 'Check for updates again before installing this release.',
    target_changed: 'The available release changed. Check again before installing.',
    update_in_progress: 'An update is already running. Its progress appears here.',
  };
  return Object.hasOwn(known, code) ? known[code] : (/^[a-z0-9_:-]{1,120}$/.test(code) ? code.replace(/[_:]+/g, ' ') : 'The updater needs attention. Check the server update report.');
}

/** Real API orchestration; apply is never retried after a lost acknowledgement. */
export class HermesUpdateClient {
  view: HermesUpdateView = { status: null, action: null, error: '', reconnecting: false, errorKind: null };
  private generation = 0;
  private reading = false;
  private applyingTarget: string | null = null;
  private listeners = new Set<(view: HermesUpdateView) => void>();
  constructor(private request: <T>(path: string, options?: RequestInit) => Promise<T>) {}
  subscribe(listener: (view: HermesUpdateView) => void) { this.listeners.add(listener); listener(this.view); return () => { this.listeners.delete(listener); }; }
  private set(change: Partial<HermesUpdateView>) { this.view = { ...this.view, ...change }; this.listeners.forEach(fn => fn(this.view)); }
  async refresh() {
    if (this.reading || this.view.action) return;
    this.reading = true; const generation = this.generation;
    try {
      const status = await this.request<HermesUpdateStatus>('/updates/status');
      if (generation === this.generation) {
        const acknowledged = this.applyingTarget === status.target_commit && status.operation?.kind === 'apply';
        const clear = this.view.errorKind === 'connection' || acknowledged;
        if (acknowledged) this.applyingTarget = null;
        this.set({ status, reconnecting: false, ...(clear ? { error: '', errorKind: null } : {}) });
      }
    } catch {
      if (generation === this.generation) this.set({ reconnecting: true, ...(this.view.error ? {} : { error: 'Cannot reach Hermes. This screen will reconnect; an accepted update continues on the server.', errorKind: 'connection' }) });
    } finally { this.reading = false; }
  }
  async check() {
    if (this.view.action || updateActive(this.view.status)) return;
    const generation = ++this.generation;
    this.set({ action: 'checking', error: '', errorKind: null, reconnecting: false });
    try {
      const status = await this.request<HermesUpdateStatus>('/updates/check', { method: 'POST', body: '{}', signal: AbortSignal.timeout(60000) });
      if (generation === this.generation) this.set({ status });
    } catch { if (generation === this.generation) this.set({ error: 'Could not check for updates. Your current service and saved work are unchanged. Try checking again.', errorKind: 'action' }); }
    finally { if (generation === this.generation) this.set({ action: null }); }
  }
  async apply() {
    if (this.view.action || !canApplyUpdate(this.view.status)) return;
    const { target_commit, check_id } = this.view.status;
    this.applyingTarget = target_commit;
    const generation = ++this.generation;
    this.set({ action: 'applying', error: '', errorKind: null, reconnecting: false });
    try {
      const status = await this.request<HermesUpdateStatus>('/updates/apply', { method: 'POST', body: JSON.stringify({ expected_commit: target_commit, check_id }), signal: AbortSignal.timeout(30000) });
      if (generation === this.generation) { this.applyingTarget = null; this.set({ status }); }
    } catch {
      if (generation === this.generation) this.set({ reconnecting: true, error: 'The update request lost its connection. Checking its saved server status before any further action.', errorKind: 'action' });
    } finally { if (generation === this.generation) this.set({ action: null }); }
  }
}
