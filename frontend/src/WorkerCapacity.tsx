import { Layers3 } from 'lucide-react';
import { count, type ExecutionCapacity } from './api';

export default function WorkerCapacity({ capacity }: { capacity: ExecutionCapacity | null }) {
  if (!capacity) return <section className="panel worker-capacity" aria-label="Worker capacity"><strong>Worker capacity unavailable</strong><p>Reconnect to the service to read its active browser limit.</p></section>;
  return <section className="panel worker-capacity" aria-label="Worker capacity">
    <div className="worker-capacity-title"><Layers3 size={18} /><h2>Worker capacity</h2></div>
    <p><strong>Up to {count(capacity.effective_browser_page_limit)} browser products at once</strong> · {count(capacity.product_task_limit)} total product tasks per instance</p>
    {typeof capacity.active_browser_workers === 'number' && <p aria-live="polite">{count(capacity.active_browser_workers)} browser workers active · {count(capacity.waiting_for_browser_workers)} waiting for a browser worker</p>}
    <p>Browser tab limit: {count(capacity.browser_page_limit)}. Product URLs beyond this limit wait for a tab. Each source’s concurrent-request limit also caps its active browser pages. Supplier request limits and backoff are shared across tabs.</p>
    <details><summary>Change the browser worker limit</summary><p>Set <code>{capacity.browser_setting}=99</code> in <code>.env</code> or the server environment, then restart Hermes. The supported range is 1–{count(capacity.maximum_configured_limit)}; the product task limit also applies. Start with a small number and check machine memory before opening many product pages.</p></details>
  </section>;
}
