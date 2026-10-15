import { count, type Product } from './api';

const knownCount = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? count(value) : 'Not assessed';

export default function ProductRowCounts({ product }: { product: Product }) {
  // The product API exposes retained row counts, not an exhaustive configuration
  // domain or an aggregate verification count. Never infer either from status.
  return <div className="product-row-counts" role="group" aria-label={`${product.title || 'Product'} row counts`}>
    <dl>
      <div className="current-row-count"><dt>Current extraction rows</dt><dd>{knownCount(product.scoped_row_count)}</dd></div>
      <div><dt>Historical rows · legacy</dt><dd>{knownCount(product.legacy_row_count)}</dd></div>
      <div className="configuration-total"><dt>Final valid configurations</dt><dd>Unknown</dd></div>
    </dl>
    <small>{count(product.row_count)} total retained records · final domain not confirmed</small>
  </div>;
}
