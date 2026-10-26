import { useSyncExternalStore } from 'react';
import { monotonicNow } from './productTimingState';

/** One shared timer for all visible live durations; no network work per product. */
export function createTimingClock(read = monotonicNow, every = globalThis.setInterval, cancel = globalThis.clearInterval) {
  const listeners = new Set<() => void>();
  let value = read(); let timer: ReturnType<typeof setInterval> | undefined;
  const pulse = () => { value = read(); listeners.forEach(listener => listener()); };
  return {
    snapshot: () => value,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (timer === undefined) { value = read(); timer = every(pulse, 1000); }
      return () => { listeners.delete(listener); if (!listeners.size && timer !== undefined) { cancel(timer); timer = undefined; } };
    },
  };
}
const shared = createTimingClock();
const inactive = () => () => {};
const zero = () => 0;
export function useTimingTick(active: boolean): number {
  return useSyncExternalStore(active ? shared.subscribe : inactive, shared.snapshot, zero);
}
