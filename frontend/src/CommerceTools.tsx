import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowRight, Calculator, CalendarClock, Info, LoaderCircle, Plus, Trash2, TriangleAlert, Truck, X } from 'lucide-react';
import { errorMessage, humanize, money, post } from './api';

type Tool = 'shipping' | 'production';
type Package = { weight: string; length: string; width: string; height: string };
type Result = { status: string; reason?: string; missing_inputs?: string[]; missing_prerequisites?: string[]; [key: string]: unknown };
const emptyPackage = (): Package => ({ weight: '', length: '', width: '', height: '' });

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}
function Message({ error }: { error: string }) {
  return error ? <div className="error-box" role="alert"><TriangleAlert size={16} /><span>{error}</span></div> : null;
}
function EstimateResult({ result }: { result: Result }) {
  const complete = ['estimate', 'estimated'].includes(result.status);
  const dateRange = (value: unknown) => {
    if (!value || typeof value !== 'object') return 'Not calculated';
    const range = value as { earliest: string; latest: string };
    return range.earliest === range.latest ? range.earliest : `${range.earliest} – ${range.latest}`;
  };
  return <div className={`commerce-result ${complete ? '' : 'commerce-result-pending'}`} role="status"><div className="commerce-result-heading"><span className="eyebrow">{complete ? 'CALCULATED ESTIMATE' : 'INPUTS NEED ATTENTION'}</span><span className="status">{humanize(result.status)}</span></div>{result.amount !== undefined && result.amount !== null && <div className="commerce-amount">{money(String(result.amount), String(result.currency))}</div>}{result.reason && <p>{result.reason}</p>}{result.missing_inputs?.length ? <p>Needed: {result.missing_inputs.join(', ')}</p> : null}{result.missing_prerequisites?.length ? <p>Pending: {result.missing_prerequisites.map(humanize).join(', ')}</p> : null}{result.production_start ? <dl className="detail-list"><div><dt>Production starts</dt><dd>{String(result.production_start)}</dd></div><div><dt>Production completes</dt><dd>{dateRange(result.production_complete)}</dd></div><div><dt>{(result.arrival as { type?: string })?.type === 'pickup_ready_estimate' ? 'Pickup ready' : 'Arrival estimate'}</dt><dd>{dateRange(result.arrival)}</dd></div><div><dt>Calendar timezone</dt><dd>{String(result.timezone)}</dd></div><div><dt>Seasonal / holiday extra days</dt><dd>{String(result.seasonal_or_holiday_extra_days ?? 0)}</dd></div></dl> : null}{result.billable_weight ? <dl className="detail-list"><div><dt>Billable weight</dt><dd>{String(result.billable_weight)} {String((result.context as Record<string, unknown>)?.weight_unit || '')}</dd></div>{Object.entries((result.components as Record<string, unknown>) || {}).map(([key, value]) => <div key={key}><dt>{humanize(key)}</dt><dd>{money(String(value), String(result.currency))}</dd></div>)}</dl> : null}{result.calculation_basis ? <p>{String(result.calculation_basis)}</p> : null}<p className="estimate-disclaimer"><Info size={13} />{result.quote_type ? 'Based on the tariff and package measurements you supplied. This is not a live supplier or carrier quote.' : 'Conditional calendar estimate, not a guaranteed delivery date or a rush-fee quote.'}</p>{Array.isArray(result.limitations) && <details className="estimate-limitations"><summary>Calculation limitations</summary><ul>{result.limitations.map((limitation, index) => <li key={index}>{String(limitation)}</li>)}</ul></details>}</div>;
}

function ShippingCalculator() {
  const [currency, setCurrency] = useState('USD');
  const [weightUnit, setWeightUnit] = useState('lb');
  const [zone, setZone] = useState('');
  const [service, setService] = useState('');
  const [base, setBase] = useState('');
  const [perWeight, setPerWeight] = useState('');
  const [increment, setIncrement] = useState('');
  const [dimensional, setDimensional] = useState(false);
  const [dimensionUnit, setDimensionUnit] = useState('in');
  const [divisor, setDivisor] = useState('');
  const [packages, setPackages] = useState<Package[]>([emptyPackage()]);
  const [residential, setResidential] = useState(false);
  const [perPackage, setPerPackage] = useState('');
  const [handling, setHandling] = useState('');
  const [residentialFee, setResidentialFee] = useState('');
  const [fuelPercent, setFuelPercent] = useState('');
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const updatePackage = (index: number, key: keyof Package, value: string) => setPackages((current) => current.map((item, position) => position === index ? { ...item, [key]: value } : item));
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setResult(null); setBusy(true);
    try {
      const rate = { zone: zone.trim(), service: service.trim(), base, per_weight: perWeight, per_package: perPackage || '0', handling: handling || '0', residential_surcharge: residentialFee || '0', fuel_percent: fuelPercent || '0' };
      const tariff = { currency: currency.toUpperCase(), weight_unit: weightUnit, weight_increment: increment, rates: [rate], ...(dimensional ? { dimensional_divisor: divisor, dimension_unit: dimensionUnit } : {}) };
      const inputs = { zone: zone.trim(), service: service.trim(), residential, weight_unit: weightUnit, packages: packages.map((item) => dimensional ? item : { weight: item.weight }), ...(dimensional ? { dimension_unit: dimensionUnit } : {}) };
      setResult(await post<Result>('/commerce/shipping', { tariff, inputs }));
    } catch (error) { setError(errorMessage(error)); } finally { setBusy(false); }
  };
  return <form onSubmit={submit} onChange={() => setResult(null)} className="settings-form commerce-form"><fieldset className="commerce-fields" disabled={busy}><div className="inline-note"><Info size={18} /><div><strong>Use your actual tariff and package measurements.</strong><p>All rates are supplied by you. This tool calculates one shipment and does not retrieve a live shipping quote.</p></div></div><div className="form-columns"><Field label="Currency"><input required minLength={3} maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} pattern="[A-Za-z]{3}" /></Field><Field label="Weight unit"><select value={weightUnit} onChange={(e) => setWeightUnit(e.target.value)}><option value="lb">Pounds · lb</option><option value="kg">Kilograms · kg</option><option value="oz">Ounces · oz</option><option value="g">Grams · g</option></select></Field></div><div className="form-columns"><Field label="Destination zone"><input required value={zone} onChange={(e) => setZone(e.target.value)} placeholder="Zone from your tariff" /></Field><Field label="Shipping service"><input required value={service} onChange={(e) => setService(e.target.value)} placeholder="Service from your tariff" /></Field></div><div className="form-columns"><Field label={`Base charge · ${currency}`}><input type="number" min="0" step="any" required value={base} onChange={(e) => setBase(e.target.value)} placeholder="Enter base charge" /></Field><Field label={`Charge per ${weightUnit} · ${currency}`}><input type="number" min="0" step="any" required value={perWeight} onChange={(e) => setPerWeight(e.target.value)} placeholder="Enter rate per weight unit" /></Field></div><Field label={`Billable weight rounding increment · ${weightUnit}`} hint="Each package is rounded upward to this tariff increment; use the value specified by your tariff."><input type="number" min="0.000001" step="any" required value={increment} onChange={(e) => setIncrement(e.target.value)} placeholder="Enter your tariff's weight increment" /></Field><label className="checkbox-field commerce-checkbox"><input type="checkbox" checked={dimensional} onChange={(e) => setDimensional(e.target.checked)} /><span>Use dimensional weight when it exceeds actual weight</span></label>{dimensional && <div className="form-columns"><Field label="Dimension unit"><select value={dimensionUnit} onChange={(e) => setDimensionUnit(e.target.value)}><option value="in">Inches · in</option><option value="cm">Centimeters · cm</option><option value="mm">Millimeters · mm</option><option value="m">Meters · m</option></select></Field><Field label="Dimensional divisor" hint={`Must convert ${dimensionUnit}³ into ${weightUnit}.`}><input type="number" min="0.000001" step="any" required value={divisor} onChange={(e) => setDivisor(e.target.value)} placeholder="Enter tariff divisor" /></Field></div>}<div className="commerce-subheading"><h3>Packages in this shipment</h3><button type="button" className="text-link" onClick={() => { setPackages((items) => [...items, emptyPackage()]); setResult(null); }}><Plus size={13} />Add package</button></div><div className="packages-list">{packages.map((item, index) => <div key={index} className="package-row"><div className="package-label">Package {index + 1}</div><div className="package-fields"><Field label={`Actual weight · ${weightUnit}`}><input type="number" min="0.000001" step="any" required value={item.weight} onChange={(e) => updatePackage(index, 'weight', e.target.value)} placeholder="Weight" /></Field>{dimensional && (['length', 'width', 'height'] as const).map((key) => <Field key={key} label={`${humanize(key)} · ${dimensionUnit}`}><input type="number" min="0.000001" step="any" required value={item[key]} onChange={(e) => updatePackage(index, key, e.target.value)} placeholder={humanize(key)} /></Field>)}<button type="button" className="icon-button" disabled={packages.length === 1} onClick={() => { setPackages((items) => items.filter((_, position) => position !== index)); setResult(null); }} aria-label={`Remove package ${index + 1}`}><Trash2 size={15} /></button></div></div>)}</div><label className="checkbox-field commerce-checkbox"><input type="checkbox" checked={residential} onChange={(e) => setResidential(e.target.checked)} /><span>Residential destination</span></label><details className="advanced-details"><summary>Optional tariff components</summary><p className="form-explainer">Blank optional charges are treated as zero. Fuel applies only to base, weight, and per-package charges.</p><div className="form-columns"><Field label={`Per-package fee · ${currency}`}><input type="number" min="0" step="any" value={perPackage} onChange={(e) => setPerPackage(e.target.value)} placeholder="Optional" /></Field><Field label={`Handling fee · ${currency}`}><input type="number" min="0" step="any" value={handling} onChange={(e) => setHandling(e.target.value)} placeholder="Optional" /></Field><Field label={`Residential surcharge · ${currency}`}><input type="number" min="0" step="any" value={residentialFee} onChange={(e) => setResidentialFee(e.target.value)} placeholder="Optional" /></Field><Field label="Fuel surcharge · percent"><input type="number" min="0" step="any" value={fuelPercent} onChange={(e) => setFuelPercent(e.target.value)} placeholder="Optional" /></Field></div></details><Message error={error} />{result && <EstimateResult result={result} />}<div className="modal-footer"><span>Tariff-based estimate · no carrier request</span><button disabled={busy} className="button button-primary">{busy ? <LoaderCircle size={15} className="spin" /> : <Calculator size={15} />}Calculate shipping</button></div></fieldset></form>;
}

function ProductionCalculator() {
  const [processed, setProcessed] = useState('');
  const [ordered, setOrdered] = useState('');
  const [productionMin, setProductionMin] = useState('');
  const [productionMax, setProductionMax] = useState('');
  const [transitMin, setTransitMin] = useState('');
  const [transitMax, setTransitMax] = useState('');
  const [approved, setApproved] = useState(false);
  const [paid, setPaid] = useState(false);
  const [rush, setRush] = useState(false);
  const [pickup, setPickup] = useState(false);
  const [holidays, setHolidays] = useState('');
  const [indeterminate, setIndeterminate] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setResult(null); setBusy(true);
    try {
      for (const value of [processed, ...(rush ? [ordered] : [])]) if (!/(Z|[+-]\d{2}:\d{2})$/i.test(value.trim()) || !Number.isFinite(Date.parse(value))) throw new Error('Use an ISO date/time with an explicit timezone, such as 2026-09-25T08:00:00-07:00.');
      if (productionMax && Number(productionMax) < Number(productionMin)) throw new Error('Maximum production days cannot be below the minimum.');
      if (!pickup && transitMax && (!transitMin || Number(transitMax) < Number(transitMin))) throw new Error('Provide a transit minimum no greater than the maximum.');
      const closures = holidays.split(/[\s,]+/).filter(Boolean);
      if (closures.some((value) => !/^\d{4}-\d{2}-\d{2}$/.test(value))) throw new Error('Additional closure dates must use YYYY-MM-DD.');
      setResult(await post<Result>('/commerce/production', { processed_at: processed.trim(), production_days: [Number(productionMin), Number(productionMax || productionMin)], transit_days: !pickup && transitMin ? [Number(transitMin), Number(transitMax || transitMin)] : null, approved, paid, rush, ordered_at: rush ? ordered.trim() : null, pickup, holidays: closures, indeterminate_services: indeterminate ? ['design_or_mailing'] : [] }));
    } catch (error) { setError(errorMessage(error)); } finally { setBusy(false); }
  };
  return <form onSubmit={submit} onChange={() => setResult(null)} className="settings-form commerce-form"><fieldset className="commerce-fields" disabled={busy}><div className="inline-note"><Info size={18} /><div><strong>Printograph production calendar</strong><p>Uses Pacific-time processing cutoffs, business days, and published seasonal rules. Enter the source's production and transit durations; it does not infer rush fees.</p></div></div><Field label="Processed timestamp · ISO with timezone" hint="The order's processing time after approval and payment, including its UTC offset."><input required value={processed} onChange={(e) => setProcessed(e.target.value)} placeholder="2026-09-25T08:00:00-07:00" /></Field><div className="form-columns"><Field label="Production business days · minimum"><input type="number" min={1} step={1} required value={productionMin} onChange={(e) => setProductionMin(e.target.value)} placeholder="From supplier" /></Field><Field label="Production business days · maximum" hint="Leave blank for the same value as the minimum."><input type="number" min={1} step={1} value={productionMax} onChange={(e) => setProductionMax(e.target.value)} placeholder="Optional upper bound" /></Field></div><div className="commerce-checks"><label className="checkbox-field"><input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} /><span>Artwork approved</span></label><label className="checkbox-field"><input type="checkbox" checked={paid} onChange={(e) => setPaid(e.target.checked)} /><span>Payment complete</span></label><label className="checkbox-field"><input type="checkbox" checked={rush} onChange={(e) => setRush(e.target.checked)} /><span>Rush processing requested</span></label><label className="checkbox-field"><input type="checkbox" checked={pickup} onChange={(e) => setPickup(e.target.checked)} /><span>Local pickup</span></label></div>{rush && <Field label="Order placed timestamp · ISO with timezone" hint="Required to check the separate rush ordering cutoff."><input required value={ordered} onChange={(e) => setOrdered(e.target.value)} placeholder="2026-09-25T04:30:00-07:00" /></Field>}{!pickup && <div className="form-columns"><Field label="Transit business days · minimum" hint="Optional; without it, only production dates are calculated."><input type="number" min={0} step={1} value={transitMin} onChange={(e) => setTransitMin(e.target.value)} placeholder="From selected carrier service" /></Field><Field label="Transit business days · maximum"><input type="number" min={0} step={1} value={transitMax} onChange={(e) => setTransitMax(e.target.value)} placeholder="Optional upper bound" /></Field></div>}<details className="advanced-details"><summary>Additional calendar constraints</summary><Field label="Additional closure dates" hint="One YYYY-MM-DD date per line. Published source holidays are already included."><textarea rows={3} value={holidays} onChange={(e) => setHolidays(e.target.value)} placeholder="YYYY-MM-DD" /></Field><label className="checkbox-field"><input type="checkbox" checked={indeterminate} onChange={(e) => setIndeterminate(e.target.checked)} /><span>Includes design or mailing services with an unknown duration</span></label></details><Message error={error} />{result && <EstimateResult result={result} />}<div className="modal-footer"><span>Conditional estimate · America/Los_Angeles</span><button disabled={busy} className="button button-primary">{busy ? <LoaderCircle size={15} className="spin" /> : <CalendarClock size={15} />}Calculate dates</button></div></fieldset></form>;
}

function CommerceDialog({ tool, onClose }: { tool: Tool; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; ref.current?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const focusable = [...(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select,textarea,summary') || [])].filter((item) => item.getClientRects().length);
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { last?.focus(); event.preventDefault(); }
        if (!event.shiftKey && document.activeElement === last) { first?.focus(); event.preventDefault(); }
      }
    };
    document.addEventListener('keydown', handle);
    return () => { document.removeEventListener('keydown', handle); document.body.style.overflow = overflow; previous?.focus(); };
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><div className="modal modal-wide" ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={id}><div className="modal-heading"><div><div className="eyebrow">COMMERCE CALCULATORS</div><h2 id={id}>{tool === 'shipping' ? 'Estimate shipping from your tariff' : 'Calculate a production window'}</h2></div><button className="icon-button" aria-label="Close calculator" onClick={onClose}><X size={20} /></button></div>{tool === 'shipping' ? <ShippingCalculator /> : <ProductionCalculator />}</div></div>;
}

export default function CommerceTools() {
  const [tool, setTool] = useState<Tool | null>(null);
  const close = useCallback(() => setTool(null), []);
  return <><section className="panel sources-panel"><div className="panel-top"><div className="panel-title"><Calculator size={19} /><h2>Commerce calculators</h2><span className="optional-badge">Estimates</span></div></div><p className="section-description">Use explicit rates, package measurements, and supplier durations. Calculated estimates remain separate from live supplier quotes.</p><div className="commerce-tool-cards"><button onClick={() => setTool('shipping')}><span className="source-icon"><Truck size={22} /></span><div><strong>Shipping estimator</strong><p>Package weights, tariff rates, and optional surcharges.</p></div><ArrowRight size={17} /></button><button onClick={() => setTool('production')}><span className="source-icon"><CalendarClock size={22} /></span><div><strong>Production & arrival</strong><p>Printograph business days, rush cutoffs, and transit.</p></div><ArrowRight size={17} /></button></div></section>{tool && <CommerceDialog tool={tool} onClose={close} />}</>;
}
