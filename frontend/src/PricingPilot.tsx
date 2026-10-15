import { useEffect, useState } from 'react';
import { api, post, errorMessage, humanize, type ProductDiagnosisRecord } from './api';

type PriceTier = {qty:number|[number,number]; price:string; full_sheet_price?:boolean};
type Observation = {quantity:number; status:string; quote:{unit_price:string; subtotal:string; price_break_status?:string; price_break_tiers?:PriceTier[]}; dom:{turnaround:{label:string}}};
type Pilot = {id:string; diagnosis_id:string; diagnosis_revision:string|number; status:string; quote_requests:number; quantities:number[]; observations:Observation[]; warnings:string[]; catalog_complete:false};
const running = (pilot:Pilot|null) => !!pilot && ['opening','running'].includes(pilot.status);

export default function PricingPilot({record}:{record:ProductDiagnosisRecord}) {
  const [pilot,setPilot] = useState<Pilot|null>(null), [error,setError] = useState(''), [busy,setBusy] = useState(false);
  const [retry,setRetry] = useState(0);
  useEffect(() => {
    let disposed=false;
    setPilot(null); setError('');
    void api<{items:Pilot[]}>('/pricing-pilots').then(data => {
      if (!disposed) setPilot(data.items.find(item => item.diagnosis_id === record.id && item.diagnosis_revision === record.revision) || null);
    }).catch(failure => { if (!disposed) setError(errorMessage(failure)); });
    return () => { disposed=true; };
  },[record.id,record.revision,retry]);
  useEffect(() => {
    if (!running(pilot)) return;
    let disposed=false;
    const timer=window.setTimeout(() => {
      void api<Pilot>(`/pricing-pilots/${encodeURIComponent(pilot!.id)}`).then(next => {
        if (!disposed) { setPilot(next); setError(''); }
      }).catch(failure => { if (!disposed) setError(errorMessage(failure)); });
    },2000);
    return () => { disposed=true; window.clearTimeout(timer); };
  },[pilot]);
  const start=async () => {
    if (busy || running(pilot) || record.revision == null) return;
    setBusy(true); setError('');
    try { setPilot(await post<Pilot>(`/product-diagnoses/${encodeURIComponent(record.id)}/pricing-pilot`,{revision:record.revision,quantities:[1,10,100,101]})); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  };
  const money=(value:string) => new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(Number(value));
  const tierResponses=pilot?.observations.filter(row=>row.quote.price_break_status==='source_numeric_tiers' && row.quote.price_break_tiers?.length) || [];
  const unresolvedTiers=pilot?.observations.some(row=>row.quote.price_break_status==='unresolved_schema');
  return <section className="diagnosis-finalization" aria-label="Supplier pricing check">
    <h3>Supplier pricing check</h3>
    <p>Read the supplier’s pricing response for quantities 1, 10, 100, and 101. Uses a fresh page to recheck the saved configuration with Email Proof off and shipping unselected.</p>
    <button className="button button-secondary button-small" disabled={busy || running(pilot) || !record.selection} onClick={() => void start()}>{busy || running(pilot) ? 'Checking supplier pricing…' : 'Check pricing breaks'}</button>
    {error && <div className="diagnosis-error" role="alert">{error} <button className="text-link" onClick={() => setRetry(value=>value+1)}>Refresh check status</button></div>}
    {pilot && <div role="status"><p><strong>{humanize(pilot.status)}</strong> · {pilot.observations.length} of {pilot.quantities.length} quantity checks · {pilot.quote_requests} quote requests</p>
      {!!pilot.observations.length && <div style={{overflowX:'auto'}}><table className="data-table"><thead><tr><th>Quantity</th><th>Unit price</th><th>Subtotal</th><th>Production</th><th>Comparison</th></tr></thead><tbody>{pilot.observations.map(row=><tr key={row.quantity}><td>{row.quantity}</td><td>{money(row.quote.unit_price)}</td><td>{money(row.quote.subtotal)}</td><td>{row.dom.turnaround.label}</td><td>{row.status === 'matched' ? 'Matches page and quote' : 'Needs review'}</td></tr>)}</tbody></table></div>}
      {tierResponses.length>0 ? <details><summary>Supplier-provided quantity tiers</summary>{tierResponses.map(row=><div key={row.quantity}><strong>Response for quantity {row.quantity}</strong><div style={{overflowX:'auto'}}><table className="data-table" aria-label={`Supplier quantity tiers for response ${row.quantity}`}><thead><tr><th>Quantity</th><th>Supplier price</th><th>Price basis</th></tr></thead><tbody>{row.quote.price_break_tiers!.map((tier,index)=><tr key={index}><td>{Array.isArray(tier.qty) ? tier.qty.join('–') : tier.qty}</td><td>{money(tier.price)}</td><td>{tier.full_sheet_price === true ? 'Full sheet' : tier.full_sheet_price === false ? 'Not a full-sheet price' : 'Not specified'}</td></tr>)}</tbody></table></div></div>)}</details> : pilot.observations.length>0 && !unresolvedTiers && <p>No quantity tiers were returned in these responses.</p>}
      {unresolvedTiers && <p className="diagnosis-attention">Some quantity-tier responses have an unresolved format and need review.</p>}
      {pilot.observations.length>0 && <p>Sample comparisons do not establish every price break.</p>}
      {!!pilot.warnings.length && <ul>{pilot.warnings.map(warning=><li key={warning}>{humanize(warning)}</li>)}</ul>}
      <p>Limited configuration check. Shipping, tax, full catalog coverage, and final product eligibility remain unverified.</p>
    </div>}
  </section>;
}
