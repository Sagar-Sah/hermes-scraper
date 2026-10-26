import { memo, useMemo } from 'react';
import { Clock3 } from 'lucide-react';
import { canTickTiming, monotonicNow, timingView, type ProductTimingData } from './productTimingState';
import { useTimingTick } from './timingClock';
import './ProductTiming.css';

function ProductTiming({ timing, live = true, label = 'Product collection time' }: { timing?: ProductTimingData | null; live?: boolean; label?: string }) {
  // Anchor each received server sample once. Wall-clock changes cannot add hours.
  const receivedAt = useMemo(monotonicNow, [timing]);
  const eligible = canTickTiming(timing, live);
  const current = monotonicNow();
  const stillLive = eligible && (current - receivedAt) / 1000 < timing!.live_until! - timing!.server_now;
  const tick = useTimingTick(stillLive);
  const view = timingView(timing, eligible && !stillLive ? current : tick, receivedAt, live);
  return <details className={`product-timing ${view.partial ? 'product-timing-partial' : ''}`}>
    <summary aria-label={`${label}: ${view.tooltip}`} title={view.tooltip}>
      <span className="product-timing-duration"><Clock3 size={12} aria-hidden="true" />{view.label}</span>
      <small>{view.caption}</small>
    </summary>
    <div className="product-timing-breakdown"><dl>{view.details.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl><p>{view.explanation}</p><p>{view.history}</p>{view.warning && <p>{view.warning}</p>}</div>
  </details>;
}
export default memo(ProductTiming);
