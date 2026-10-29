export function evidenceMoney(value: unknown, notApplicable = false): string {
  if (notApplicable) return 'Not applicable';
  if (typeof value !== 'string' && typeof value !== 'number') return '—';
  const text = String(value).trim();
  return /^(?:\d+|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?$/.test(text) ? `$${text}` : '—';
}

export function evidenceSelection(selection: Record<string, unknown>): string {
  const labels: Record<string, string> = { quantity: 'Qty', colorspec: 'Printing',
    stakes_per_sign: 'H-stakes per sign', flute_direction: 'Flute direction' };
  const parts: string[] = [];
  if (selection.width !== undefined && selection.height !== undefined) {
    parts.push(`Width ${selection.width} × Height ${selection.height}`);
  }
  for (const [key, value] of Object.entries(selection)) {
    if (key === 'product_id' || (['width', 'height'].includes(key) && selection.width !== undefined && selection.height !== undefined)) continue;
    const label = labels[key] || key.replace(/_/g, ' ').replace(/^./, letter => letter.toUpperCase());
    parts.push(`${label} ${value}`);
  }
  return parts.join(' · ') || '—';
}

export interface RenderedEvidence {
  selection: Record<string, unknown>; case_id?: string; site_cost?: string;
  expected?: string; expected_retail?: string; displayed_retail?: string; match?: boolean;
  observed?: { total_text?: string; button_disabled?: boolean; warning_text?: string };
}

export function evidenceExpectedResult(item: RenderedEvidence): string {
  return item.expected?.startsWith('blocked') ? 'Blocked orientation' : item.expected === 'price' ? 'Price' : 'Price check';
}

export function evidenceDisplayed(item: RenderedEvidence): string {
  const blocked = item.observed?.button_disabled === true;
  const price = evidenceMoney(item.displayed_retail);
  const rawShown = item.observed?.total_text?.trim();
  const shown = price !== '—' ? price : rawShown && /^\$[\d,.]+$/.test(rawShown) ? rawShown : undefined;
  return blocked ? `Blocked${shown ? ` · shown ${shown}` : ''}` : shown || '—';
}

export function evidenceCaseResult(item: RenderedEvidence): string {
  const matched = item.match === true ? 'Match' : item.match === false ? 'Mismatch' : 'Unknown';
  return item.match === true && item.expected?.startsWith('blocked') && item.observed?.button_disabled === true
    ? `${matched} · blocked as expected` : matched;
}
