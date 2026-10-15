/** Presentation only: raw selections and supplier IDs remain in row evidence. */
export const customerOptionColumns = [
  { label: 'Size', keys: ['size'] },
  { label: 'Shape', keys: ['shape'] },
  { label: 'Stock', keys: ['stock', 'paper'] },
  { label: 'Colorspec', keys: ['colorspec', 'color', 'colors'] },
  { label: 'Coating', keys: ['coating', 'finish'] },
] as const;

const scalar = (value: unknown): value is string | number | boolean => ['string', 'number', 'boolean'].includes(typeof value);

export function customerValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  // Numeric scalar attributes can be real sizes or custom measurements.
  if (scalar(value)) return String(value);
  if (Array.isArray(value)) return value.map(customerValue).join(', ') || '—';
  const item = value as Record<string, unknown>;
  if (scalar(item.label) && String(item.label).trim()) return String(item.label);
  const unit = typeof item.unit === 'string' ? item.unit.trim() : '';
  if (unit && scalar(item.value)) return `${item.value} ${unit}`;
  if (unit && scalar(item.width) && scalar(item.height)) return `${item.width} × ${item.height} ${unit}`;
  if (!Object.keys(item).length) return '—';
  // An unlabeled selection object may contain an opaque supplier ID. Do not
  // turn it into a customer label or guess a label from its numeric value.
  return 'Label unavailable';
}

export function customerConfigurationValue(configuration: Record<string, unknown>, aliases: readonly string[]): string {
  const entries = Object.entries(configuration);
  const normalized = (key: string) => key.replace(/^attribute_(?:pa_)?|^pa_/, '').replace(/[ _-]/g, '').toLowerCase();
  for (const alias of aliases) {
    const match = entries.find(([key]) => normalized(key) === alias);
    if (match) return customerValue(match[1]);
  }
  return '—';
}
