declare const __HERMES_UI_BUILD__: string;

export interface BuildHealth {
  feature_version: string;
  features: string[];
  runtime_backend_sha256: string;
  source_backend_sha256: string;
  restart_required: boolean;
  frontend: { built_source_sha256: string | null; rebuild_required: boolean; assets_valid: boolean };
}

export const loadedDashboardBuild = typeof __HERMES_UI_BUILD__ === 'string' ? __HERMES_UI_BUILD__ : 'development';

export function buildWarning(build: BuildHealth | undefined, loaded = loadedDashboardBuild): string | null {
  if (!build) return 'The running backend does not report its build. Open Update Hermes to check and load the current release.';
  if (build.restart_required) return 'The running backend is older than these files. Open Update Hermes to install and restart safely.';
  if (build.frontend.rebuild_required) return 'The dashboard build is outdated or incomplete. Open Update Hermes to rebuild and load the current controls.';
  if (loaded !== 'development' && loaded !== build.frontend.built_source_sha256) return 'A newer dashboard is ready. Reload this page to display the current controls.';
  return null;
}

export function BuildNotice({ build, received, onUpdate }: { build: BuildHealth | undefined; received: boolean; onUpdate?: () => void }) {
  const warning = received ? buildWarning(build) : null;
  const reload = !!build && !build.restart_required && !build.frontend.rebuild_required && loadedDashboardBuild !== 'development' && loadedDashboardBuild !== build.frontend.built_source_sha256;
  return warning ? <div className="connection-banner" role="alert"><strong>Update needed</strong><span>{warning}</span>{onUpdate && <button className="button button-secondary hermes-update-trigger" onClick={reload ? () => window.location.reload() : onUpdate}>{reload ? 'Reload dashboard' : 'Update Hermes'}</button>}</div> : null;
}

export default function BuildStatus({ build }: { build: BuildHealth | undefined }) {
  return <span className="build-status" title={build ? `${build.feature_version}; ${build.features.join(', ')}` : 'Waiting for backend build identity'}>
    UI {loadedDashboardBuild.slice(0, 12)} · API {build?.runtime_backend_sha256.slice(0, 12) || 'unknown'}
  </span>;
}
