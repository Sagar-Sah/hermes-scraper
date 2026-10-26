import type { ProductTimingData } from './productTimingState';
export interface CollectionProduct {
  id: string; product_id?: string; run_id: string; run_name?: string; title?: string; url?: string;
  status: string; coverage_status?: string; row_count: number; complete: boolean;
  timing?: ProductTimingData;
  progress?: number | null; progress_percent?: number | null; active?: boolean; exportable?: boolean;
}
export interface CollectionArchive {
  id: string; product_id?: string; title?: string; filename: string; url?: string; download_url?: string;
  size?: number; created_at?: string; complete?: boolean; row_count?: number;
}
export interface CollectionExport { id: string; filename: string; url: string; size?: number; status?: string; products: CollectionProduct[]; finalize_selection?: string[]; cleanup?: { status: string } }
export interface CollectionSnapshot { items: CollectionProduct[]; archives: CollectionArchive[]; pending_exports?: CollectionExport[] }
export const collectionProductId = (product: Pick<CollectionProduct, 'id' | 'product_id'>) => product.product_id || product.id;
export const completedProductIds = (products: CollectionProduct[]) => products.filter(product => product.complete === true).map(collectionProductId);
export function collectionProgress(product: CollectionProduct): number | null {
  if (product.complete === true) return 100;
  if (typeof product.progress === 'number' && Number.isFinite(product.progress) && product.progress >= 0 && product.progress <= 1) return product.progress * 100;
  if (typeof product.progress_percent === 'number' && Number.isFinite(product.progress_percent) && product.progress_percent >= 0 && product.progress_percent <= 100) return product.progress_percent;
  return null;
}
export function reconcileCollectionSelection(selected: Set<string>, products: CollectionProduct[], selectCompleted: boolean): Set<string> {
  const available = new Set(products.filter(product => product.exportable !== false).map(collectionProductId));
  const next = new Set([...selected].filter(id => available.has(id)));
  if (selectCompleted) for (const product of products) if (product.complete === true && available.has(collectionProductId(product))) next.add(collectionProductId(product));
  return next;
}
