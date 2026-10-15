export type RunStatus = 'queued' | 'discovering' | 'running' | 'harvesting' | 'verifying' | 'paused' | 'completed' | 'failed' | 'cancelled' | string;
export interface Product {
  id: string; url: string; title: string; status: RunStatus; row_count: number;
  coverage_status: string; error?: string; request_count: number; retry_at?: string | null;
  retry?: { attempt?: number; message?: string; code?: string } | null;
  record_scope?: string; legacy_row_count?: number | null; scoped_row_count?: number | null;
}
export interface Run {
  id: string; name: string; status: RunStatus; created_at: string; updated_at: string;
  total_products: number; completed_products: number; row_count: number; request_count: number;
  cache_hits: number; error_count: number; coverage_status: string; progress?: number;
  finished_attempts?: number; partial_products?: number; active_products?: number; attention_products?: number;
  configuration_total?: number | null; eta_seconds?: number | null;
  record_scope?: string; legacy_row_count?: number | null; scoped_row_count?: number | null;
  error?: string; products?: Product[]; adapter?: string; fixture?: boolean;
}
export type CampaignStage = 'queued' | 'diagnosed' | 'extracting' | 'extracted' | 'verified' | 'finalized' | 'blocked' | 'excluded';
export interface CampaignSummary {
  id: string; name: string; total_products: number; status: string; updated_at: string;
  source_pages_collected?: number; public_variants_collected?: number; account_baselines_collected?: number; fourover_default_probes_collected?: number; fourover_price_tables_collected?: number; fourover_option_maps_collected?: number; fourover_area_drafts_collected?: number; fourover_rendered_canary_observations?: number; itoris_option_tables_collected?: number; itoris_quantity_tables_collected?: number; dps_route_price_scopes_collected?: number; b2sign_scoped_drafts_collected?: number; b2sign_scoped_draft_rows?: number; fourover_shipping_baselines_collected?: number; active_products?: number; excluded_products?: number; original_total_products?: number;
  counts: { diagnosed: number; extracted: number; verified: number; finalized: number; blocked: number };
  active_group_index?: number;
  active_group_product_ids?: string[];
  group_progress?: { number: number; product_ids: string[]; product_count: number; excluded_count: number; required_count: number; finalized_count: number; held_count: number; status: 'complete' | 'active' | 'queued' }[];
  alternate_supplier_jobs?: { id: string; storefront_product_id: string; storefront_title: string; storefront_url: string | null; supplier: string; source_title: string; supplier_url: string | null; stage: string; reason: string | null; queued_at: string }[];
}
export interface CampaignProduct {
  id: string; title: string; storefront_url: string; supplier_url: string | null; supplier: string | null;
  stage: CampaignStage; reason: string | null; diagnosis_id: string | null; run_id: string | null;
  product_id: string | null; finalization_id: string | null; row_count: number; updated_at: string;
  source_collection_id?: string; source_collection_status?: string; exclusion_reason?: string; excluded_at?: string;
  diagnosis_status?: string; diagnosis_field_count?: number; diagnosis_warnings?: string[];
  public_variant_evidence?: { variant_count: number; price_basis: string; collected_at: string };
  account_baseline_evidence?: { comparison: string; observed_at: string; final_eligible: false };
  fourover_default_evidence?: { tier_count: number; observed_at: string; final_eligible: false };
  fourover_price_table_evidence?: { child_count: number; priced_count: number; tier_count: number; complete_child_routes: boolean; observed_at: string; final_eligible: false };
  fourover_option_map_evidence?: { mapped_children: number; expected_children: number; complete_child_mapping: boolean; observed_at: string; final_eligible: false };
  fourover_rows_evidence?: { row_count: number; verification: string; observed_at: string; final_eligible: false };
  fourover_area_rows_evidence?: { row_count: number; coverage: string; verification: string; observed_at: string; final_eligible: false };
  itoris_option_evidence?: { field_count: number; choice_count: number; priced_choice_count: number; fee_label_conflict_count: number; collected_at: string; final_eligible: false };
  itoris_quantity_evidence?: { tier_count: number; first_tier_matches_account_baseline: boolean | null; collected_at: string; final_eligible: false };
  dps_route_price_evidence?: { quoted_count: number; planned_count: number; held_count: number; coverage: string; verification: string; observed_at: string; final_eligible: false };
  b2sign_scoped_draft_evidence?: { row_count: number; coverage: string; verification: string; observed_at: string; final_eligible: false };
  fourover_shipping_baseline_evidence?: { method_count: number; scope: string; observed_at: string; final_eligible: false };
  finalization_evidence?: { pricing_model?: string; quoted_canaries?: number; source_data_complete?: boolean; storefront_deployed?: boolean };
}
export interface CampaignDetail extends CampaignSummary { products: CampaignProduct[] }
export interface B2PricingHarvest {
  id: string; title: string; url: string; status: string;
  requested_count: number; completed_count: number; row_count: number; invalid_count: number; quote_requests: number;
  finite_requested_set_exhausted: boolean; catalog_complete: boolean; verification_status: string;
  warnings?: string[]; updated_at?: string;
}
export interface DPSPricingPilot {
  id: string; url: string; status: string; requested_quantities: number[];
  observations: unknown[]; quote_requests: number; warnings?: string[]; updated_at?: string;
  catalog_complete: boolean; final_eligible: boolean;
}
export interface Row {
  id: string; product_url: string; title: string; source_product_id: string;
  configuration: Record<string, unknown>; quantity: number; currency: string;
  subtotal: string | number | null; unit_price: string | number | null;
  provenance: string; verification_status: string; evidence_ids: string[];
  production: Record<string, unknown>; images: (string | Record<string, unknown>)[];
  coverage_status: string; pricing_context?: Record<string, unknown>;
  account_context?: unknown; tax_basis?: unknown; quantity_unit?: string; units_per_pack?: unknown;
  discount_context?: unknown; services?: unknown; snapshot_id?: unknown;
  record_scope?: string; price_comparison_context?: Record<string, unknown> | null;
}
export interface VariationChoice { key?: string; label: string }
export interface VariationCounts { candidate_rows?: number | null; valid_rows?: number | null; priced_rows?: number | null }
export interface VariationSizePlan extends VariationCounts { key?: string; label: string; notes?: string[] }
export interface VariationProductPlan {
  product_id: string; product_type?: string; title: string; product_url?: string; status?: string;
  counts?: VariationCounts;
  table_subtotal_rows?: number | null;
  inventory?: { sizes?: VariationChoice[]; stocks?: VariationChoice[]; colorspecs?: VariationChoice[]; corners?: VariationChoice[]; quantities?: (number | VariationChoice)[]; other_dimensions?: { label: string; choices: VariationChoice[]; notes?: string }[] };
  size_plans?: VariationSizePlan[];
  non_cost_variants?: { label: string; choices: VariationChoice[]; notes?: string }[];
  notes?: string[];
}
export interface VariationPlanPage { products: VariationProductPlan[]; notes?: string[] }
export interface SupplierSession {
  id: string; name: string; origin: string; entry_url: string; custom?: boolean;
  status: 'not_open' | 'signed_in' | 'login_required' | 'unknown' | 'opening' | 'error' | 'closed' | 'attention'; message?: string;
}
export interface NewSupplierSession { name: string; website_url: string; login_url?: string }
export interface DiagnosisField {
  key: string; label: string; kind: string;
  choices: { value: string; label: string; disabled: boolean; placeholder?: boolean }[];
  selected_values: string[] | null; required: boolean; disabled: boolean; configurable: boolean;
  multiple?: boolean; min?: string | null; max?: string | null; step?: string | null;
  binding_kind?: 'display_label_only' | 'native_control'; value_origin?: 'display_label_surrogate' | 'native_value'; warnings?: string[];
}
export interface DiagnosisSelectionField { key: string; include: boolean; fixed_values: string[] }
export interface DiagnosisSelection { revision: string | number; fields: DiagnosisSelectionField[] }
export interface DiagnosisLaunchProduct { diagnosis_id: string; revision: string | number; fields: DiagnosisSelectionField[] }
export interface DiagnosisPreflight {
  ready: boolean; plan_token?: string | null;
  products: { diagnosis_id: string; title: string; status: 'ready' | 'blocked'; reasons: string[] }[];
  planned_products: number; pricing_request_count: number | null; eta_seconds: number | null; note?: string;
}
export interface DiagnosisLaunchResult { batch_id: string; runs: Run[] }
export interface ProductDiagnosisRecord {
  id: string; url: string; title?: string; created_at?: string;
  status: 'opening' | 'analyzing' | 'ready' | 'login_required' | 'attention' | 'error' | 'closed';
  revision?: string | number; fields?: DiagnosisField[]; warnings?: string[];
  coverage?: 'current_page_only'; supplier_id?: string; session_status?: SupplierSession['status'];
  selection?: DiagnosisSelection | null; message?: string;
}
export interface RunEvent { id: number; created_at: string; type: string; payload: Record<string, unknown> }
export interface EventPage { items: RunEvent[]; has_older?: boolean | null; has_newer?: boolean; next_before?: number | null; next_after?: number | null }
export const attentionCount = (run: Run) => run.attention_products ?? run.error_count ?? 0;
export const needsAttention = (run: Run) => attentionCount(run) > 0 || ['needs_attention', 'access_required', 'failed'].includes(run.status);
export function authenticationAttention(events: RunEvent[], run: Run): RunEvent[] {
  if (['completed', 'completed_with_gaps', 'cancelled', 'paused', 'exporting', 'failed'].includes(run.status)) return [];
  const waiting = new Map<string, RunEvent>();
  const chronological = [...events].sort((a, b) => {
    const left = Number(a.id), right = Number(b.id);
    return Number.isFinite(left) && Number.isFinite(right) ? left - right : Date.parse(a.created_at) - Date.parse(b.created_at);
  });
  for (const event of chronological) {
    const productId = typeof event.payload.product_id === 'string' ? event.payload.product_id : '';
    const resets = ['run_resume', 'run_pause', 'run_cancel', 'resume'].includes(event.type)
      || (event.type === 'run_control' && ['resume', 'pause', 'cancel'].includes(String(event.payload.action || '')));
    if (resets) { if (productId) waiting.delete(productId); else waiting.clear(); continue; }
    if (!productId) continue;
    if (event.type === 'attention' && event.payload.code === 'access_required') waiting.set(productId, event);
    else if (['discovery', 'progress', 'product_finished', 'product_resumed', 'product_resume'].includes(event.type)) waiting.delete(productId);
  }
  return [...waiting.values()].filter((event) => {
    const product = run.products?.find((item) => item.id === event.payload.product_id);
    return !product || !['completed', 'partial', 'cancelled', 'failed'].includes(product.status);
  });
}
export interface ExportFile {
  name: string; url: string; size?: number; snapshot_row_count?: number; current_row_count?: number;
  created_at?: string | null; generation?: string | null; stale?: boolean;
  product_status?: string | null; current_coverage_status?: string; verification_claim?: string;
  record_scope?: string; legacy_row_count?: number | null; scoped_row_count?: number | null;
}
export interface Source { id: string; name?: string; domain?: string; base_url?: string; url?: string; adapter?: string; concurrency?: number; requests_per_second?: number; status?: string; [key: string]: unknown }
export interface Provider { id: string; name: string; base_url: string; model: string; has_api_key?: boolean }
export interface NewRun {
  name: string; urls: string[]; titles?: string[]; source_url?: string; instructions?: string;
  adapter: 'auto' | 'browser' | 'printograph' | 'printitprints' | 'woocommerce' | 'fixture'; image_mode: 'urls' | 'download' | 'both';
  provider_id?: string;
  browser_bulk_quantities?: boolean;
  refresh?: boolean; max_requests?: number; max_configurations?: number;
  filters?: Record<string, unknown>; shipping_contexts?: unknown[];
  source_metadata?: { document_transport?: 'browser' | 'http' };
  fixture_products?: number; fixture_rows_per_product?: number;
}

export const getToken = () => sessionStorage.getItem('atlas_api_token') || '';
export const setToken = (token: string) => { if (token) sessionStorage.setItem('atlas_api_token', token); else sessionStorage.removeItem('atlas_api_token'); };
export async function importUrls(file: File): Promise<string[]> {
  const token = getToken(); const body = new FormData(); body.append('file', file);
  const response = await fetch('/api/v1/import-urls', { method: 'POST', body, headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!response.ok) { const result = await response.json().catch(() => null); throw new Error(friendlyDetail(result?.detail || `Import failed (${response.status}).`)); }
  return (await response.json()).urls || [];
}
export async function downloadExport(file: ExportFile, options?: { timeoutMs: number }) {
  const token = getToken();
  if (new URL(file.url, window.location.origin).origin !== window.location.origin) throw new Error('Export downloads must be served by this Hermes workspace.');
  const response = await fetch(file.url, { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: options ? AbortSignal.timeout(options.timeoutMs) : undefined });
  if (!response.ok) throw new Error(`Download failed (${response.status}). Check your session token.`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = file.name;
  document.body.appendChild(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function friendlyDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((value) => typeof value === 'object' && value ? `${value.loc?.slice(1).join('.') || 'Input'}: ${value.msg || JSON.stringify(value)}` : String(value)).join('; ');
  if (detail && typeof detail === 'object') {
    const value = detail as Record<string, unknown>;
    const message = String(value.message || value.explanation || value.error || 'The request needs attention.');
    return value.matches ? `${message} ${JSON.stringify(value.matches)}` : message;
  }
  return String(detail);
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    signal: options?.signal ?? ((!options?.method || options.method.toUpperCase() === 'GET') ? AbortSignal.timeout(12000) : undefined),
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options?.headers },
  });
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      const body = JSON.parse(text);
      message = friendlyDetail(body.detail ?? body.error ?? body);
    } catch { /* Text responses are displayed directly. */ }
    throw new Error(message || `Request failed (${response.status}).`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const post = <T>(path: string, value?: unknown) => api<T>(path, { method: 'POST', body: value === undefined ? undefined : JSON.stringify(value) });
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' });
export const isActive = (status: string) => ['queued', 'discovering', 'running', 'harvesting', 'verifying', 'exporting'].includes(status);
export const displayValue = (value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value !== 'object') return String(value);
  if (Array.isArray(value)) return value.map(displayValue).join(', ');
  const option = value as Record<string, unknown>;
  if (option.label !== null && option.label !== undefined) return displayValue(option.label);
  if (option.value !== null && option.value !== undefined) return displayValue(option.value);
  return JSON.stringify(value);
};
export const count = (value: number | undefined) => new Intl.NumberFormat('en-US').format(value ?? 0);
export const compactCount = (value: number) => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
export const humanize = (value?: string) => (value || 'unknown').replaceAll('_', ' ').replace(/\b\w/g, (v) => v.toUpperCase());
export const money = (value: string | number | null | undefined, currency = 'USD', digits?: number) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency, ...(digits === undefined ? {} : { maximumFractionDigits: digits, minimumFractionDigits: digits }) }).format(Number(value)); }
  catch { return `${Number(value).toFixed(digits ?? 2)} ${currency}`; }
};
export const stableJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(',')}}`;
  return JSON.stringify(value ?? null);
};
export const dateTime = (value: string) => new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value));
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

/** A finite HTTP stage, separate from catalog completion and price verification. */
export interface SourceDomainStatus {
  job_id: string; run_id: string; scope: string; status: string; run_paused?: boolean;
  planned_unique_requests?: number | null; completed_requests?: number | null;
  remaining_unique_requests?: number | null; attempts_used?: number | null; attempt_budget?: number | null;
  task_counts?: { pending?: number; leased?: number; succeeded?: number; failed?: number };
  known_stage_requests_complete?: boolean; catalog_complete?: boolean;
  final_configuration_count?: number | null; updated_at?: string | number | null;
  task_errors?: { request_key: string; error: string }[];
}
export interface SourceDomainPage { items: SourceDomainStatus[] }
