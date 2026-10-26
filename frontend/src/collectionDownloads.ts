import { getToken } from './api';
interface WritableFile { write(data: Uint8Array): Promise<void>; close(): Promise<void>; abort?(): Promise<void> }
export interface ExportDirectory {
  name?: string;
  getFileHandle(name: string, options: { create: boolean }): Promise<{ createWritable(): Promise<WritableFile> }>;
}
export type FolderPicker = () => Promise<ExportDirectory>;
export const folderPicker = (): FolderPicker | undefined => {
  const picker = (window as Window & { showDirectoryPicker?: (options: { mode: 'readwrite' }) => Promise<ExportDirectory> }).showDirectoryPicker;
  return window.isSecureContext && picker ? () => picker.call(window, { mode: 'readwrite' }) : undefined;
};
export const cancelledFileChoice = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
function filename(value: string): string {
  if (!value || value.length > 240 || /[\\/\x00-\x1f]/.test(value) || value === '.' || value === '..') throw new Error('The export filename is invalid. Your collection was kept.');
  return value;
}
export async function saveCollectionDownload(url: string, name: string, expectedSize?: number, directory?: ExportDirectory): Promise<'saved' | 'browser_download'> {
  const target = new URL(url, window.location.origin);
  if (target.origin !== window.location.origin || target.username || target.password || target.hash || !target.pathname.startsWith('/api/v1/collection/')) throw new Error('Collection downloads must come from this Hermes workspace.');
  const safeName = filename(name), token = getToken();
  const response = await fetch(target.href, { headers: token ? { Authorization: `Bearer ${token}` } : {}, redirect: 'error', signal: AbortSignal.timeout(120000) });
  if (!response.ok) throw new Error(`The ZIP could not be downloaded (${response.status}). Your collection was kept.`);
  const checkSize = (size: number) => {
    if (typeof expectedSize === 'number' && Number.isSafeInteger(expectedSize) && expectedSize >= 0 && size !== expectedSize) throw new Error('The download was incomplete. Retry the saved export; your collection was kept.');
  };
  if (directory) {
    const handle = await directory.getFileHandle(safeName, { create: true });
    const writable = await handle.createWritable();
    try {
      let size = 0;
      if (response.body) {
        const reader = response.body.getReader();
        try { while (true) { const part = await reader.read(); if (part.done) break; size += part.value.byteLength; await writable.write(part.value); } }
        catch (error) { try { await reader.cancel(); } catch { /* Preserve original stream error. */ } throw error; }
        finally { reader.releaseLock(); }
      } else { const data = new Uint8Array(await response.arrayBuffer()); size = data.byteLength; await writable.write(data); }
      checkSize(size); await writable.close(); return 'saved';
    } catch (error) { try { await writable.abort?.(); } catch { /* Preserve original save error. */ } throw error; }
  }
  const blob = await response.blob(); checkSize(blob.size);
  const objectUrl = URL.createObjectURL(blob), link = document.createElement('a'); link.href = objectUrl; link.download = safeName;
  document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  // Starting a browser download is not evidence that its file was saved.
  return 'browser_download';
}
